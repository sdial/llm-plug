import pytest
import asyncio
import json
from pathlib import Path


@pytest.fixture(scope="session")
def fixtures_dir():
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def anthropic_request():
    fixtures_dir = Path(__file__).parent / "fixtures"
    with open(fixtures_dir / "anthropic_request.json") as f:
        return json.load(f)


@pytest.fixture
def openai_chat_request():
    fixtures_dir = Path(__file__).parent / "fixtures"
    with open(fixtures_dir / "openai_chat_request.json") as f:
        return json.load(f)


@pytest.fixture
def openai_response_request():
    fixtures_dir = Path(__file__).parent / "fixtures"
    with open(fixtures_dir / "openai_response_request.json") as f:
        return json.load(f)


@pytest.fixture
def mock_channels():
    fixtures_dir = Path(__file__).parent / "fixtures"
    with open(fixtures_dir / "mock_channels.json") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
