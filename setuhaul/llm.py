"""Language model access through AWS Bedrock's Converse API (tool use).

Configuration (environment):
  BEDROCK_MODEL_ID   required: a Claude model or inference-profile ID enabled in your account,
                     copied from the Bedrock console (Model catalog / Cross-region inference)
  AWS_REGION         region where that model is enabled, e.g. us-east-1 or ap-south-1
  AWS credentials    the usual boto3 chain: AWS_PROFILE, env keys, or SSO

ScriptedModel stands in for Bedrock in tests: it replays a fixed list of responses.
"""
from __future__ import annotations

import os
from typing import Protocol


class ModelUnavailable(Exception):
    pass


class Model(Protocol):
    def converse(self, *, system: str, messages: list, tool_config: dict) -> dict: ...


class BedrockModel:
    def __init__(self, model_id: str | None = None, region: str | None = None):
        self.model_id = model_id or os.environ.get("BEDROCK_MODEL_ID")
        self.region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not self.model_id:
            raise ModelUnavailable("BEDROCK_MODEL_ID is not set. See README: Configure Bedrock.")
        import boto3
        self._client = boto3.client("bedrock-runtime", region_name=self.region)

    def converse(self, *, system: str, messages: list, tool_config: dict) -> dict:
        try:
            return self._client.converse(
                modelId=self.model_id,
                system=[{"text": system}],
                messages=messages,
                toolConfig=tool_config,
                inferenceConfig={"maxTokens": 1024, "temperature": 0.2},
            )
        except Exception as e:  # botocore errors: credentials, access denied, throttling
            raise ModelUnavailable(f"Bedrock call failed: {e}") from e


class ScriptedModel:
    """Replays responses. Each step is either ('text', str) or ('tool', name, input_dict)."""

    def __init__(self, steps: list[tuple]):
        self.steps = list(steps)
        self.calls: list[list] = []

    def converse(self, *, system: str, messages: list, tool_config: dict) -> dict:
        self.calls.append(messages)
        kind, *rest = self.steps.pop(0)
        if kind == "tool":
            name, args = rest
            block = {"toolUse": {"toolUseId": f"tu{len(self.calls)}", "name": name, "input": args}}
            return {"output": {"message": {"role": "assistant", "content": [block]}}, "stopReason": "tool_use"}
        return {"output": {"message": {"role": "assistant", "content": [{"text": rest[0]}]}},
                "stopReason": "end_turn"}


_model: Model | None = None


def get_model() -> Model:
    global _model
    if _model is None:
        _model = BedrockModel()
    return _model


def set_model(model: Model | None) -> None:
    global _model
    _model = model
