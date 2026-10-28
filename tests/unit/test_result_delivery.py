from src.interface.result_delivery import extract_result_delivery


def test_extracts_whatsapp_delivery_suffix():
    goal, delivery = extract_result_delivery(
        "Research the route. When finished, send the final result to me on WhatsApp")
    assert goal == "Research the route"
    assert delivery.channel == "whatsapp"


def test_extracts_slack_delivery_suffix_without_changing_task():
    goal, delivery = extract_result_delivery(
        "Summarize the report and send the result to Slack.")
    assert goal == "Summarize the report"
    assert delivery.channel == "slack"


def test_unrelated_message_is_unchanged():
    goal, delivery = extract_result_delivery("Send the report to the printer")
    assert goal == "Send the report to the printer"
    assert delivery is None


def test_webui_chat_attaches_delivery_request_without_running_network(tmp_path):
    from src.interface.triggers import TriggerDispatcher
    from src.interface.webui import WebUIServer

    seen = {}
    dispatcher = TriggerDispatcher()
    dispatcher.register("webui", lambda event: seen.update(
        goal=event.goal, delivery=event.payload.get("result_delivery")))
    server = WebUIServer(tmp_path, dispatcher=dispatcher)

    result = server.handle_chat({
        "message": "Check the files and send the result to Slack"})
    assert result["reply"] == "Received."
    assert seen == {"goal": "Check the files", "delivery": {"channel": "slack"}}
