"""Bedrock is not called in tests. botocore's Stubber validates our Converse request against the
real service model, so a malformed tool config or message would fail here."""
import boto3
from botocore.stub import ANY, Stubber

from setuhaul import agent, llm


def test_converse_requests_match_bedrock_schema(con, monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "example-model-id")
    model = llm.BedrockModel(region="us-east-1")
    client = model._client
    stub = Stubber(client)
    params = {"modelId": "example-model-id", "system": ANY, "messages": ANY, "toolConfig": ANY,
              "inferenceConfig": ANY}
    tool_turn = {"output": {"message": {"role": "assistant", "content": [
                    {"toolUse": {"toolUseId": "t1", "name": "get_my_shipments", "input": {}}}]}},
                 "stopReason": "tool_use", "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                 "metrics": {"latencyMs": 1}}
    text_turn = {"output": {"message": {"role": "assistant", "content": [{"text": "Which shipment?"}]}},
                 "stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                 "metrics": {"latencyMs": 1}}
    stub.add_response("converse", tool_turn, params)
    stub.add_response("converse", text_turn, params)   # second call carries the toolResult message
    with stub:
        tid = agent.start_thread(con, "DRV004")
        r = agent.handle_message(tid, "late by 45 minutes", model=model)
    stub.assert_no_pending_responses()
    assert r["reply"] == "Which shipment?"
