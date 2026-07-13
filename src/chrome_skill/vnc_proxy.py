import logging

logger = logging.getLogger(__name__)


def create_vnc_url_request(user_guid="", user_qua2="", app="", session_id=""):
    """
    Stub: Create VNC URL request payload.
    TODO: Implement actual VNC proxy URL request construction.
    """
    req = {
        "userbase": {
            "guid": user_guid,
            "qua2": user_qua2
        },
        "app": app,
        "session_id": session_id
    }
    return req


def get_vnc_proxy_url(origin_url, user_guid="", user_qua2="", app="", session_id=""):
    """
    Stub: Get VNC proxy URL - returns origin_url directly without proxy.
    TODO: Implement actual VNC proxy URL retrieval from backend service.
    """
    logger.info(f"get_vnc_proxy_url: stub implementation, returning origin_url [{origin_url}]")
    return origin_url


def main():
    print("vnc_proxy: stub implementation, no VNC proxy functionality available")


if __name__ == "__main__":
    main()