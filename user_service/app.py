from flask import Flask, jsonify

app = Flask(__name__)

@app.route('/user')
def get_user():
    user_data = {
        "id": 1,
        "name": "John Doe",
        "email": "john.doe@example.com"
    }
    return jsonify(user_data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
