import os
from abc import ABC
import json
from dotenv import load_dotenv
import requests
import modal

load_dotenv()

TORCHSERVE_URL = os.getenv("TORCHSERVE_URL")
MODEL_CLIENT = os.getenv("MODEL_CLIENT")


class TimeoutException(Exception):
    """Raised when a client exceeds its execution duration limit and times out."""


class ModelClient(ABC):

    def send(self, app_name, message):
        pass


class ModalModelClient(ModelClient):

    def send(self, app_name, message):
        try:
            generate = modal.Function.from_name(app_name, "CrystaLLMModel.generate")
            result = generate.remote(inputs=message)
            return result
        except modal.exception.TimeoutError:
            raise TimeoutException("the request to the model timed out")


class TorchserveClient(ModelClient):

    def __init__(self, url):
        self._url = url

    def send(self, app_name, message):
        response = requests.post(
            self._url,
            data=json.dumps(message),
        )
        return response.json()


def get_model_client():
    if MODEL_CLIENT == "modal":
        return ModalModelClient()
    elif MODEL_CLIENT == "torchserve":
        return TorchserveClient(TORCHSERVE_URL)
    else:
        raise Exception(f"unexpected model client: {MODEL_CLIENT}")
