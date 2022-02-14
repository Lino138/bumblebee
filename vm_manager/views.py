import django_rq
import logging

from datetime import datetime, timedelta, timezone
from math import ceil

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.forms.models import model_to_dict
from django.http import HttpResponseRedirect, Http404
from django.template import loader
from django.urls import reverse
from django.utils.html import format_html
from django.views.decorators.csrf import csrf_exempt

from operator import itemgetter

from researcher_desktop.models import DesktopType, AvailabilityZone
from researcher_desktop.utils.utils import get_desktop_type

from vm_manager.models import VMStatus, Instance, Resize, Volume

from vm_manager.constants import VM_ERROR, VM_OKAY, VM_WAITING, \
    VM_SHELVED, NO_VM, VM_SHUTDOWN, VM_SUPERSIZED, VM_DELETED, \
    VM_CREATING, VM_MISSING, VM_RESIZING, LAUNCH_WAIT_SECONDS, \
    CLOUD_INIT_FINISHED, CLOUD_INIT_STARTED, REBOOT_WAIT_SECONDS, \
    RESIZE_WAIT_SECONDS, SHELVE_WAIT_SECONDS, \
    REBOOT_SOFT, REBOOT_HARD, SCRIPT_ERROR, SCRIPT_OKAY, \
    BOOST_BUTTON, EXTEND_BUTTON, EXTEND_BOOST_BUTTON
from vm_manager.utils.expiry import BoostExpiryPolicy, InstanceExpiryPolicy
from vm_manager.utils.utils import after_time
from vm_manager.utils.utils import generate_hostname

# These are all needed, as they're consumed by researcher_workspace/views.py
from vm_manager.vm_functions.admin_functionality import test_function, \
    admin_worker, start_downsizing_cron_job, \
    vm_report_for_page, vm_report_for_csv, db_check
from vm_manager.vm_functions.create_vm import launch_vm_worker, extend_instance
from vm_manager.vm_functions.delete_vm import delete_vm_worker, delete_volume
from vm_manager.vm_functions.other_vm_functions import reboot_vm_worker
from vm_manager.vm_functions.shelve_vm import shelve_vm_worker, \
    unshelve_vm_worker
from vm_manager.vm_functions.resize_vm import supersize_vm_worker, \
    downsize_vm_worker, extend_boost

logger = logging.getLogger(__name__)


def _wrong_state_message(action, user, feature=None, desktop_type=None,
                         vm_status=None, vm_id=None):
    status_str = f"in wrong state ({vm_status.status})" if vm_status else "missing"
    instance_str = (
        f", instance {vm_id}" if vm_id
        else f", instance {vm_status.instance.id}" if (vm_status
                                                       and vm_status.instance)
        else "")
    kind_str = (
        f", desktop_type {desktop_type.id}" if desktop_type
        else f", feature {feature}" if feature else "")
    message = (
        f"VMStatus for user {user}{kind_str}{instance_str} is {status_str}. "
        f"Cannot {action} VM.")
    logger.error(message)
    return message


def launch_vm(user, desktop_type, zone) -> str:
    # TODO - the handling of race conditions (below) is dodgy

    vm_statuses = VMStatus.objects.get_latest_vm_statuses(user)
    if vm_statuses:
        message = f"User {user} already has {len(vm_statuses)} live desktops"
        logger.error(message)
        return message

    vm_status = VMStatus(
        user=user, requesting_feature=desktop_type.feature,
        operating_system=desktop_type.id, status=VM_CREATING,
        wait_time=after_time(LAUNCH_WAIT_SECONDS),
        status_progress=0, status_message="Starting desktop creation"
    )
    vm_status.save()

    # Check for race condition in previous statements and delete
    # duplicate VMStatus
    check_vm_status = \
        VMStatus.objects.filter(user=user,
                                operating_system=desktop_type.id,
                                requesting_feature=desktop_type.feature) \
                        .exclude(status__in=[NO_VM, VM_SHELVED])
    if check_vm_status.count() > 1:
        vm_status.delete()
        error_message = f"A VMStatus with that User and OS already exists"
        logger.error(error_message)
        return error_message

    queue = django_rq.get_queue('default')
    queue.enqueue(launch_vm_worker, user=user, desktop_type=desktop_type,
                  zone=zone)

    return str(vm_status)


def delete_vm(user, vm_id, requesting_feature) -> str:
    try:
        vm_status = VMStatus.objects.get_vm_status_by_untrusted_vm_id(
            vm_id, user, requesting_feature)
    except VMStatus.DoesNotExist:
        vm_status = None

    if not vm_status or vm_status.status in (NO_VM, VM_DELETED):
        return _wrong_state_message(
            "delete", user, feature=requesting_feature, vm_status=vm_status)

    logger.info(f"Changing the VMStatus of {vm_id} from {vm_status.status} "
                f"to {VM_DELETED} and Mark for Deletion is set on the "
                f"Instance and Volume {vm_status.instance.boot_volume.id}")
    # This is currently done out of the sight of the user.  Progress
    # is not displayed.
    vm_status.status = VM_DELETED
    vm_status.save()
    vm_status.instance.set_marked_for_deletion()
    vm_status.instance.boot_volume.set_marked_for_deletion()

    queue = django_rq.get_queue('default')
    queue.enqueue(delete_vm_worker, vm_status.instance)

    return str(vm_status)


@login_required(login_url='login')
def admin_delete_vm(request, vm_id):
    if not request.user.is_superuser:
        logger.error(f"Attempted admin delete of {vm_id} by "
                     f"non-admin user {request.user}")
        raise Http404()

    # TODO - Should be able to tidy up multiple VMstatus if found
    try:
        vm_status = VMStatus.objects.get(instance=vm_id)
    except VMStatus.DoesNotExist:
        return _wrong_state_message(
             "admin delete", request.user,
            feature="admin", vm_id=vm_id)

    logger.info(f"Performing Admin delete on {vm_id} "
                f"Mark for Deletion is set on the Instance "
                f"and Volume {vm_status.instance.boot_volume.id}")
    vm_status.status = VM_DELETED
    vm_status.save()
    vm_status.instance.set_marked_for_deletion()
    vm_status.instance.boot_volume.set_marked_for_deletion()

    queue = django_rq.get_queue('default')
    queue.enqueue(delete_vm_worker, vm_status.instance)

    logger.info(f"{request.user} admin deleted vm {vm_id}")
    return HttpResponseRedirect(reverse('admin:vm_manager_instance_change',
                                        args=(vm_id,)))


def shelve_vm(user, vm_id, requesting_feature) -> str:
    try:
        vm_status = VMStatus.objects.get_vm_status_by_untrusted_vm_id(
            vm_id, user, requesting_feature)
    except VMStatus.DoesNotExist:
        vm_status = None

    if not vm_status or vm_status.status not in [VM_OKAY, VM_SUPERSIZED]:
        return _wrong_state_message(
            "shelve", user, feature=requesting_feature, vm_status=vm_status,
            vm_id=vm_id)

    logger.info(f"Changing the VMStatus of {vm_id} "
                f"from {vm_status.status} to {VM_WAITING} "
                f"and Mark for Deletion is set on the Instance")
    vm_status.wait_time = after_time(SHELVE_WAIT_SECONDS)
    vm_status.status = VM_WAITING
    vm_status.status_progress = 0
    vm_status.status_message = "Starting desktop shelve"
    vm_status.save()
    vm_status.instance.set_marked_for_deletion()

    queue = django_rq.get_queue('default')
    queue.enqueue(shelve_vm_worker, vm_status.instance, requesting_feature)

    return str(vm_status)


def unshelve_vm(user, desktop_type) -> str:
    vm_status = VMStatus.objects.get_latest_vm_status(user, desktop_type)
    if not vm_status or vm_status.status != VM_SHELVED:
        return _wrong_state_message(
            "unshelve", user, desktop_type=desktop_type, vm_status=vm_status)
    zone = AvailabilityZone.objects.get(
        name=vm_status.instance.boot_volume.zone)

    vm_status = VMStatus(user=user,
                         requesting_feature=desktop_type.feature,
                         operating_system=desktop_type.id,
                         status=VM_CREATING,
                         wait_time=after_time(LAUNCH_WAIT_SECONDS),
                         status_progress=0,
                         status_message="Starting desktop unshelve")
    vm_status.save()

    queue = django_rq.get_queue('default')
    queue.enqueue(unshelve_vm_worker, user=user, desktop_type=desktop_type,
                  zone=zone)

    return str(vm_status)


def delete_shelved_vm(user, desktop_type) -> str:
    vm_status = VMStatus.objects.get_latest_vm_status(user, desktop_type)
    if not vm_status or vm_status.status != VM_SHELVED:
        return _wrong_state_message(
            "delete shelved", user, desktop_type=desktop_type,
            vm_status=vm_status)

    if not vm_status.instance.deleted:
        logger.error(f"Instance still exists for shelved {desktop_type}, "
                     f"vm_status: {vm_status}")
        return str(vm_status)

    vm_status.status = VM_DELETED
    vm_status.save()

    volume = Volume.objects.get_volume(user, desktop_type)
    if volume:
        logger.info(f"Deleting volume {volume}")
        delete_volume(volume)
    return str(vm_status)


def reboot_vm(user, vm_id, reboot_level, requesting_feature) -> str:
    if reboot_level not in [REBOOT_SOFT, REBOOT_HARD]:
        logger.error(f"Unrecognized reboot level ({reboot_level}) "
                     f"for instance {vm_id} and user {user}")
        # TODO - Fix the researcher_desktop layer so that we can return
        # a 400 HTTP response code.
        raise Http404
    vm_status = VMStatus.objects.get_vm_status_by_untrusted_vm_id(
        vm_id, user, requesting_feature)
    if vm_status.status not in {VM_OKAY, VM_SUPERSIZED}:
        return _wrong_state_message(
            "reboot", user, feature=requesting_feature, vm_status=vm_status,
            vm_id=vm_id)
    target_status = vm_status.status
    vm_status.status = VM_WAITING
    vm_status.wait_time = after_time(REBOOT_WAIT_SECONDS)
    vm_status.status_progress = 0
    vm_status.status_message = "Starting desktop reboot"
    vm_status.save()

    queue = django_rq.get_queue('default')
    queue.enqueue(reboot_vm_worker, user, vm_id, reboot_level,
                  target_status, requesting_feature)

    return str(vm_status)


def supersize_vm(user, vm_id, requesting_feature) -> str:
    try:
        vm_status = VMStatus.objects.get_vm_status_by_untrusted_vm_id(
            vm_id, user, requesting_feature)
    except VMStatus.DoesNotExist:
        vm_status = None

    if not vm_status or vm_status.status != VM_OKAY:
        return _wrong_state_message(
            "supersize", user, feature=requesting_feature, vm_status=vm_status,
            vm_id=vm_id)

    desktop_type = get_desktop_type(vm_status.operating_system)

    vm_status.status = VM_RESIZING
    vm_status.wait_time = after_time(RESIZE_WAIT_SECONDS)
    vm_status.status_progress = 0
    vm_status.status_message = "Starting desktop boost"
    vm_status.save()

    queue = django_rq.get_queue('default')
    queue.enqueue(supersize_vm_worker, instance=vm_status.instance,
                  desktop_type=desktop_type)

    return str(vm_status)


def downsize_vm(user, vm_id, requesting_feature) -> str:
    try:
        vm_status = VMStatus.objects.get_vm_status_by_untrusted_vm_id(
            vm_id, user, requesting_feature)
    except VMStatus.DoesNotExist:
        vm_status = None

    if not vm_status or vm_status.status != VM_SUPERSIZED:
        return _wrong_state_message(
            "downsize", user, feature=requesting_feature, vm_status=vm_status,
            vm_id=vm_id)

    desktop_type = get_desktop_type(vm_status.operating_system)

    vm_status.status = VM_RESIZING
    vm_status.wait_time = after_time(RESIZE_WAIT_SECONDS)
    vm_status.status_progress = 0
    vm_status.status_message = "Starting desktop downsize"
    vm_status.save()

    queue = django_rq.get_queue('default')
    queue.enqueue(downsize_vm_worker, instance=vm_status.instance,
                  desktop_type=desktop_type)

    return str(vm_status)


def extend_vm(user, vm_id, requesting_feature) -> str:
    try:
        vm_status = VMStatus.objects.get_vm_status_by_untrusted_vm_id(
            vm_id, user, requesting_feature)
    except VMStatus.DoesNotExist:
        vm_status = None

    if not vm_status or vm_status.status != VM_OKAY:
        return _wrong_state_message(
            "extend", user, feature=requesting_feature, vm_status=vm_status,
            vm_id=vm_id)
    expiry = extend_instance(user, vm_id, requesting_feature)
    return str(vm_status)


def extend_boost_vm(user, vm_id, requesting_feature) -> str:
    try:
        vm_status = VMStatus.objects.get_vm_status_by_untrusted_vm_id(
            vm_id, user, requesting_feature)
    except VMStatus.DoesNotExist:
        vm_status = None

    if not vm_status or vm_status.status != VM_SUPERSIZED:
        return _wrong_state_message(
            "extend_boost", user, feature=requesting_feature,
            vm_status=vm_status, vm_id=vm_id)
    expiry = extend_boost(user, vm_id, requesting_feature)

    # Also extend the instance expiry
    instance_expiry = extend_instance(user, vm_id, requesting_feature)
    return str(vm_status)


def get_vm_state(user, desktop_type):
    vm_status = VMStatus.objects.get_latest_vm_status(user, desktop_type)
    logger.debug(vm_status)

    if (not vm_status) or (vm_status.status == VM_DELETED):
        return NO_VM, "No VM", None

    instance = vm_status.instance
    if vm_status.status == VM_ERROR:
        if instance:
            return VM_ERROR, "VM has Errored", instance.id
        else:
            return VM_MISSING, "VM has Errored", None

    curr_time = datetime.now(timezone.utc)
    if vm_status.status == VM_WAITING:
        if vm_status.wait_time > curr_time:
            time = str(ceil((vm_status.wait_time - curr_time).seconds))
            return VM_WAITING, time, None
        else:  # Time up waiting
            if vm_status.instance:
                vm_status.error(f"Instance {instance.id} not ready "
                                f"at {vm_status.wait_time} timeout")
                return VM_ERROR, "Instance Not Ready", instance.id
            else:
                vm_status.status = VM_ERROR
                vm_status.save()
                logger.error(f"Instance is missing at timeout {vm_status.id}, "
                             f"{user}, {desktop_type}")
                return VM_MISSING, "VM has Errored", None

    if vm_status.status == VM_SHELVED:
        return VM_SHELVED, "VM SHELVED", instance.id

    if instance.check_shutdown_status():
        return VM_SHUTDOWN, "VM Shutdown", instance.id

    if vm_status.status == VM_OKAY:
        policy = InstanceExpiryPolicy()
        return VM_OKAY, {
            'url': instance.get_url(),
            'extension': policy.permitted_extension(instance),
            'expires': instance.expires,
            'extended_expiration': policy.new_expiry(instance)
        }, instance.id

    if not instance.check_active_or_resize_statuses():
        instance_status = instance.get_status()
        instance.error(f"Error at OpenStack level. Status: {instance_status}")
        return VM_ERROR, "Error at OpenStack level", instance.id

    if vm_status.status == VM_SUPERSIZED:
        policy = BoostExpiryPolicy()
        resize = Resize.objects.get_latest_resize(instance)
        return VM_SUPERSIZED, {
            'url': instance.get_url(),
            'extension': policy.permitted_extension(resize),
            'expires': resize.expires,
            'extended_expiration': policy.new_expiry(resize),
        }, instance.id
    logger.error(f"get_vm_state for to an unhandled state "
                 f"for {user} requesting {desktop_type}")
    raise NotImplementedError


def get_vm_status(user, desktop_type):
    vm_status = VMStatus.objects.get_latest_vm_status(user, desktop_type)
    return model_to_dict(vm_status)


def render_vm(request, user, desktop_type, buttons):
    state, what_to_show, vm_id = get_vm_state(user, desktop_type)
    app_name = desktop_type.feature.app_name

    if state == VM_SUPERSIZED:
        messages.info(request, format_html(
            f'Your {desktop_type.name} desktop is set to resize '
            f'back to the default size on {what_to_show["expires"]}'))

    # Remove buttons that are not allowed in the current context
    forbidden = []
    if state == VM_SUPERSIZED:
        forbidden.append(BOOST_BUTTON)
        forbidden.append(EXTEND_BUTTON)
        if what_to_show.get('extension').total_seconds() == 0:
            forbidden.append(EXTEND_BOOST_BUTTON)
    elif state == VM_OKAY:
        forbidden.append(EXTEND_BOOST_BUTTON)
        if what_to_show.get('extension').total_seconds() == 0:
            forbidden.append(EXTEND_BUTTON)

    context = {
        'state': state,
        'what_to_show': what_to_show,
        'desktop_type': desktop_type,
        'vm_id': vm_id,
        "buttons_to_display": [b for b in buttons if b not in forbidden],
        "app_name": app_name,
        "requesting_feature": desktop_type.feature,
        "VM_WAITING": VM_WAITING,
        "vm_status": VMStatus.objects.get_latest_vm_status(user, desktop_type),
    }

    vm_module = loader.render_to_string(f'vm_manager/html/{state}.html',
                                        context, request)
    script = loader.render_to_string(f'vm_manager/javascript/{state}.js',
                                     context, request)
    return vm_module, script, state


def notify_vm(request, requesting_feature):
    ip_address = request.GET.get("ip")
    hostname = request.GET.get("hn")
    operating_system = request.GET.get("os")
    state = int(request.GET.get("state"))
    msg = request.GET.get("msg")
    instance = Instance.objects.get_instance_by_ip_address(
        ip_address, requesting_feature)
    if not instance:
        logger.error(f"No current Instance found with "
                     f"IP address {ip_address}")
        raise Http404
    volume = instance.boot_volume
    if generate_hostname(volume.hostname_id,
                         volume.operating_system) != hostname:
        logger.error(f"Hostname provided in request does not match "
                     f"hostname of volume {instance}, {hostname}")
        raise Http404
    if state == SCRIPT_OKAY:
        if msg == CLOUD_INIT_FINISHED:
            volume.ready = True
            volume.save()
            vm_status = VMStatus.objects.get_vm_status_by_instance(
                instance, requesting_feature)
            vm_status.status = VM_OKAY
            vm_status.status_progress = 100
            vm_status.status_message = 'Instance ready'
            vm_status.save()
        elif msg == CLOUD_INIT_STARTED:
            volume.checked_in = True
            volume.save()
    else:
        vm_status = VMStatus.objects.get_vm_status_by_instance(
            instance, requesting_feature)
        vm_status.error(msg)
        logger.error(f"Notify VM Error: {msg} for instance: \"{instance}\"")
    result = f"{ip_address}, {operating_system}, {state}, {msg}"
    logger.info(result)
    return result


@csrf_exempt
def phone_home(request, requesting_feature):
    if 'instance_id' not in request.POST:
        logger.error(f"Instance ID not found in data")
        raise Http404

    instance_id = request.POST['instance_id']
    instance = Instance.objects.get_instance_by_untrusted_vm_id_2(
        request.POST['instance_id'], requesting_feature)

    vm_status = VMStatus.objects.get_vm_status_by_instance(
        instance, requesting_feature)
    if vm_status.status != VM_WAITING:
        result = (f"Unexpected phone home for {instance}. "
                  f"VM_status is {vm_status}")
        logger.error(result)
        return result

    volume = instance.boot_volume
    volume.ready = True
    volume.save()

    resize = Resize.objects.get_latest_resize(instance.id)
    status = VM_SUPERSIZED if resize and not resize.reverted else VM_OKAY

    vm_status.status_progress = 100
    vm_status.status_message = 'Instance ready'
    vm_status.status = status
    vm_status.save()
    result = f"Phone home for {instance} successful!"
    logger.info(result)
    return result


def rd_report_for_user(user, desktop_type_ids, requesting_feature):
    rd_report_info = {}
    for id in desktop_type_ids:
        vms = Instance.objects.filter(
            user=user,
            boot_volume__operating_system=id,
            boot_volume__requesting_feature=requesting_feature) \
                              .order_by('created')
        deleted = [{'date': vm.marked_for_deletion, 'count': -1}
                   for vm in vms.order_by('marked_for_deletion')
                   if vm.marked_for_deletion]
        created = [{'date': vm.created, 'count': 1} for vm in vms]
        vm_info = sorted(created + deleted, key=itemgetter('date'))
        count = 0
        vm_graph = []
        for date_obj in vm_info:
            vm_graph.append({'date': date_obj['date'], 'count': count})
            count += date_obj['count']
            date_obj['count'] = count
            vm_graph.append(date_obj)
        vm_graph.append({'date': datetime.now(timezone.utc), 'count': count})
        rd_report_info[id] = vm_graph
    return {'user_vm_info': rd_report_info}
