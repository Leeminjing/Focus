import asyncio

from plugins.spatial_patrol.docx_broker import BridgeContext, BrokerError, DocxCommandBroker


def _context():
    return BridgeContext(
        session_id="s", document_id="d", plugin_url="http://focus/plugin",
        browser_bridge_origin="http://127.0.0.1:18081",
        document_server_origin="http://127.0.0.1:18080",
    )


def test_command_requires_changed_and_version_evidence():
    async def scenario():
        broker = DocxCommandBroker()
        broker.register(_context())
        issued = asyncio.create_task(broker.issue(
            "s", action="edit", target_id="p:1", operation="replace_text",
            arguments={"text": "new"}, expected_version=3,
        ))
        command = await broker.next_command("s", timeout=0.1)
        try:
            broker.complete(command["command_id"], {"changed": True})
            raise AssertionError("missing version should fail")
        except BrokerError:
            pass
        broker.complete(command["command_id"], {"changed": True, "document_version": 4})
        result = await issued
        assert result == {"changed": True, "document_version": 4}
    asyncio.run(scenario())


def test_observation_and_projection_join_by_stable_target_id():
    broker = DocxCommandBroker()
    broker.register(_context())
    broker.publish_event("s", {
        "event": "selectionChanged",
        "target": {"target_id": "p:1", "kind": "paragraph", "content": "text"},
    })
    broker.publish_projection("s", {
        "target_id": "p:1", "page": 2,
        "rect": {"x": .1, "y": .2, "width": .3, "height": .1},
    })
    observed = broker.observe("s", "p:1")
    assert observed["content"] == "text"
    assert observed["projection"]["page"] == 2
