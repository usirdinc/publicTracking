"""RingCentral sender using the same JWT flow as V2."""
import httpx
from app import config


class DeliveryUncertain(Exception):
    pass


def configured():
    return bool(config.RINGCENTRAL_SMS_ENABLED and config.RINGCENTRAL_CLIENT_ID
                and config.RINGCENTRAL_CLIENT_SECRET and config.RINGCENTRAL_JWT
                and config.RINGCENTRAL_FROM_NUMBER)


def base_url():
    if config.RINGCENTRAL_ENVIRONMENT == "sandbox":
        return "https://platform.devtest.ringcentral.com"
    return "https://platform.ringcentral.com"


def token(client):
    response = client.post(
        "/restapi/oauth/token",
        auth=(config.RINGCENTRAL_CLIENT_ID, config.RINGCENTRAL_CLIENT_SECRET),
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
              "assertion": config.RINGCENTRAL_JWT})
    response.raise_for_status()
    return response.json()["access_token"]


def send(phone, text):
    if not configured():
        return False
    with httpx.Client(base_url=base_url(), timeout=20) as client:
        bearer = token(client)
        try:
            response = client.post(
                "/restapi/v1.0/account/~/extension/~/sms",
                headers={"Authorization": "Bearer " + bearer},
                json={"from": {"phoneNumber": config.RINGCENTRAL_FROM_NUMBER},
                      "to": [{"phoneNumber": phone}], "text": text})
        except httpx.TransportError:
            raise DeliveryUncertain() from None
        if response.status_code >= 500:
            raise DeliveryUncertain()
        response.raise_for_status()
        try:
            return bool(response.json()["id"])
        except (ValueError, KeyError):
            raise DeliveryUncertain() from None
