from models.api_types import APIType
from routers.proxy_base import make_proxy_router

router = make_proxy_router("/v1/messages", APIType.ANTHROPIC)
