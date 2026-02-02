CrystaLLM Web Application
=========================

## Installation

Required Python version: 3.10.11

To install the dependencies:
```shell
pip install -r requirements.txt
```

_NOTE_: The `requirements.txt` file is a `pip freeze` dump of the python environment on the
current server that is serving the CrystaLLM app. Some of the dependency versions may need
to be adjusted, depending on your environment. Also, some of the dependencies are old, and have vulnerabilities, 
and probably should be upgraded, especially if the app is accessible by the outside world. 

## Configuration

The app uses the `dotenv` library to load environment variables. This library expects a `.env` file to be present
in the same directory as the `app.py` script. A minimal `.env` file for this app currently would need to contain the 
following contents:
```
MODEL_CLIENT=modal
MODAL_TOKEN_ID=<your Modal token ID>
MODAL_TOKEN_SECRET=<your Modal secret>
```

## Rate limiting

The app depends on a local Redis server for tracking IP-based usage, to throttle clients, so that they are limited
to a fixed request rate.

To install Redis:
```shell
sudo apt install redis-server
sudo systemctl enable --now redis-server
```

The app itself uses the `flask_limiter` library to handle the rate limiting logic.

## Starting the App

To start the app, I use something like:
```shell
 nohup sudo ../venvs/crystaltoolkit_venv/bin/gunicorn app:server \
 -k gevent --workers 4 --timeout 180 -b 0.0.0.0:8000 > server.log 2>&1 &
```
It can also be started on port 443 with SSL support. You would need to provide the `--certfile` and 
`--keyfile` arguments, and change `0.0.0.0:8000` to `0.0.0.0:443`, to enable SSL communication.

## Logging

There are two kinds of log files produced by the app when it is started using the gunicorn command above:

*server.log*: Output of running the gunicorn command. It will contain low-level errors and 
warnings, such as problems with dependencies.

*app_logs.log*: App-level logging statements. It will contain information about each request, including IP address,
supplied inputs, and any API input and output content. This log file rolls over after it reaches a certain size, and, 
over time, you will see files like `app_logs.log.1`, `app_logs.log.2`, etc., representing older logs. The most recent 
app logs will always be in `app_logs.log`.

## API Clients

The app delegates a request for a crystal structure generation to a GPU server via an API. Currently, two GPU API 
backends are supported: Torchserve and [Modal](https://modal.com/). On the crystallm.com website, the Modal API is 
used. Using Modal requires creating an account with them, creating an app, and obtaining API credentials. You may need 
to change the contents of the `model_client.py` script to accommodate whatever GPU server backend approach you will be 
using.
