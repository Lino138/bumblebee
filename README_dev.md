## Developer Documentation


## Python package `bumblebee`
The environment and the utils for Bumblebee can be installed as python package.

- `./`: The package root.
    - `./setup.py` und `./setup.cfg`: The package install instruction.
    - `./requirements.txt`: Openstack-client and django dependencies.


## Django with Redis Queue
The frontend is [django](https://docs.djangoproject.com/en/5.0/) based.
The service uses uses the [RQ](https://github.com/rq/rq) for TODO.
The project contains the the apps: `researcher_workspace`, `researcher_desktop` and `vm_manager`.

- `./`: The django project directory
    - `./manage.py`: Handles the django cli.
- `./researcher_workspace`: Default app for TODO
    - `./researcher_workspace/migrations`: Create database structure.
    - `./researcher_workspace/settings.py`: TODO
    - `./researcher_workspace/wsg.py`: HTTP Web application.
    - `./researcher_workspace/urls.py`: The different page in the web side.
- `./researcher_desktop`: App for TODO
    - `./researcher_desktop/migrations`: Create database structure.
    - `./researcher_desktop/urls.py`: The different page in the web side.
- `./vm_manager`: App for TODO
     - `./vm_manager/urls.py`


## Docker structure:
- `./`: Docker compose root, build directory for container and source for `/app` container volume.
    - `./env-template` and `./.env`: The configuration for Bumblebee, Guacamole, Mariadb, Key-cloak and Proxy.
    - `./docker-compose.yml`: Construction plan for the services.
    - `./Dockerfile`: The build plan for TODO.
- `./docker`: Scripts for starting services within the container.
- `./docker-init`: Setup utils.
    - `docker-init/mariadb/01.sql`: Initialize the databases and a service user.
- `/docker-data`: Place to store statefull data.
    - `./docker-data/mariadb_data`: Files from the databse service.
    - `./docker-data/redis_data`: Files from the redis service.
    - `./docker-data/proxy_data`: Files from the SSL proxy service.

