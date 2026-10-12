"""Home Assistant actions for a GPT-Live client delegation.

Live's ``session.delegation.created`` event carries an id and no tool name.
This module asks a separate Responses model which Assist tool to call, runs
that tool, and returns text only after Home Assistant has answered. The Live
model speaks that text via ``session.commentary.append``.
"""

import json

PLANNER_INSTRUCTIONS = (
    "You help a voice assistant. The user transcript can be incomplete or "
    "wrong. For lights, switches, climate, media, and other home devices, "
    "call one Assist tool with complete arguments. For weather, news, facts, "
    "or anything outside the home, call web_search with the user's question. "
    "If the device, room, or action is ambiguous, do not call a tool; ask one "
    "short clarifying question. Never say that an action succeeded. The "
    "application reports success only from the tool result."
)

_COMMENTARY_LIMIT = 1500


def function_tools_from_schema(schema, allowlist: list[str] | None) -> list[dict]:
    """Turn Pipecat MCP tool schemas into Responses function tools."""
    tools = []
    if schema is None:
        return tools
    for function_schema in getattr(schema, "standard_tools", []) or []:
        name = function_schema.name
        if allowlist and name not in allowlist:
            continue
        tools.append({
            "type": "function",
            "name": name,
            "description": function_schema.description or "",
            "parameters": {
                "type": "object",
                "properties": function_schema.properties or {},
                "required": list(function_schema.required or []),
            },
        })
    return tools


class AssistDelegator:
    """Run one client delegation. Duplicate ids do not call Home Assistant again."""

    def __init__(self, complete, call_tool, tools: list[dict] | None = None):
        self._complete = complete
        self._call_tool = call_tool
        self._tools = list(tools or [])
        self._started: set[str] = set()

    def begin(self, delegation_id: str) -> bool:
        if not delegation_id or delegation_id in self._started:
            return False
        self._started.add(delegation_id)
        return True

    async def plan(self, transcript: str) -> tuple[list[tuple[str, dict]], str]:
        """Ask the planner what to do. Does not call Home Assistant."""
        response = await self._complete(transcript, self._tools)
        calls, question = _read_planner_output(response)
        if not calls:
            return [], _clip(question or "Which device should I use?")
        return calls, ""

    async def execute(self, calls: list[tuple[str, dict]]) -> str:
        """Run the named tools and return text only after each call returns."""
        parts = []
        for name, arguments in calls:
            try:
                result = await self._call_tool(name, arguments)
            except Exception as exc:
                result = f"The tool failed before Home Assistant confirmed it: {exc}"
            parts.append(f"{name}: {result}")
        return _clip(
            "Home Assistant returned the following result. "
            "Speak only what it confirmed, and do not add any other action. "
            + " ".join(parts)
        )


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) <= _COMMENTARY_LIMIT:
        return text
    return text[: _COMMENTARY_LIMIT - 1].rstrip() + "…"


def _read_planner_output(response) -> tuple[list[tuple[str, dict]], str]:
    calls = []
    texts = []
    for item in getattr(response, "output", []) or []:
        kind = getattr(item, "type", None)
        if kind == "function_call":
            raw = getattr(item, "arguments", "{}") or "{}"
            try:
                arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (TypeError, json.JSONDecodeError):
                arguments = {}
            calls.append((getattr(item, "name", ""), arguments))
        elif kind == "message":
            for part in getattr(item, "content", []) or []:
                text = getattr(part, "text", None)
                if text:
                    texts.append(text)
    return calls, " ".join(texts).strip()


async def openai_planner_complete(api_key: str, model: str, transcript: str, tools: list[dict]):
    """Responses call used in production. Tests inject their own complete()."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)
    kwargs = {
        "model": model,
        "instructions": PLANNER_INSTRUCTIONS,
        "input": transcript or "The user has not said anything yet.",
    }
    if tools:
        kwargs["tools"] = tools
    return await client.responses.create(**kwargs)
