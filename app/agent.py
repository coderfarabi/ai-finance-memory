import datetime
import json
import logging
import os
import re
import sys
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.models import Gemini
from google.adk.models.llm_response import LlmResponse
from google.adk.tools import AgentTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.workflow import START, FunctionNode, Workflow
from google.genai import types
from mcp import StdioServerParameters
from pydantic import BaseModel, Field

from app.config import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Model ──────────────────────────────────────────────────────────────────

_MODEL = Gemini(
    model=config.model,
    retry_options=types.HttpRetryOptions(
        attempts=2,
        initial_delay=30,
        max_delay=30,
        exp_base=1.0,
        http_status_codes=[429, 503],
    ),
)

# ─── Rate limiter + graceful error handler ──────────────────────────────────

_call_count = 0
_call_date = datetime.date.today()
_MAX_CALLS = config.max_api_calls_per_day

_FALLBACKS: dict[str, str] = {
    "extraction_agent": '{"date":null,"type":null,"contact":null,"category":null,"details":null,"amount":null,"due_status":null}',
    "validation_agent": '{"is_valid":false,"missing_fields":[],"clarification_question":"The AI service is busy. Please wait a moment and try your transaction again."}',
    "database_agent": '{"success":false,"message":"The AI service is busy. Please wait a moment and try again."}',
}

_ORCHESTRATOR_FALLBACK = '{"is_valid":false,"transaction":null,"clarification_question":"The AI service is busy. Please wait a moment and try your transaction again."}'

async def _rate_limit_before_model(callback_context, llm_request) -> LlmResponse | None:
    global _call_count, _call_date
    today = datetime.date.today()
    if today != _call_date:
        _call_count = 0
        _call_date = today
    if _call_count >= _MAX_CALLS:
        agent_name = callback_context.agent_name
        text = _FALLBACKS.get(agent_name, _ORCHESTRATOR_FALLBACK)
        logger.warning(json.dumps({"event": "rate_limit_blocked", "agent": agent_name, "severity": "WARNING"}))
        return LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=text)])
        )
    _call_count += 1
    return None

async def _on_model_error(callback_context, llm_request, error) -> LlmResponse | None:
    code = getattr(error, "code", None)
    if code in (429, 503):
        agent_name = callback_context.agent_name
        text = _FALLBACKS.get(agent_name, _ORCHESTRATOR_FALLBACK)
        logger.warning(json.dumps({"event": "model_error_fallback", "agent": agent_name, "code": code, "severity": "WARNING"}))
        return LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=text)])
        )
    return None

_CALLBACKS = {
    "before_model_callback": _rate_limit_before_model,
    "on_model_error_callback": _on_model_error,
}

# ─── Pydantic Schemas ───────────────────────────────────────────────────────

class ExtractedTransaction(BaseModel):
    date: str | None = Field(default=None, description="Transaction date YYYY-MM-DD. Resolve relative dates (today, yesterday) to actual dates.")
    type: str | None = Field(default=None, description="'Income' or 'Expense'.")
    contact: str | None = Field(default=None, description="Person involved, or 'None'.")
    category: str | None = Field(default=None, description="Category e.g. Food, Groceries, Salary, Loan, Travel, Rent, Medical, Shopping, Utilities, Miscellaneous.")
    details: str | None = Field(default=None, description="Short description of the transaction.")
    amount: float | None = Field(default=None, description="Monetary amount.")
    due_status: str | None = Field(default=None, description="'None', 'I Will Receive', or 'I Will Pay'.")

class ValidationResult(BaseModel):
    is_valid: bool = Field(description="True if amount, type, category, details are all present and valid.")
    missing_fields: list[str] = Field(default=[], description="List of missing critical fields.")
    clarification_question: str | None = Field(default=None, description="One concise clarification question.")

class OrchestratorOutput(BaseModel):
    is_valid: bool = Field(description="True if all critical fields are present.")
    transaction: ExtractedTransaction | None = Field(default=None)
    clarification_question: str | None = Field(default=None)

class DatabaseAgentOutput(BaseModel):
    success: bool = Field(description="True if saved successfully.")
    message: str = Field(description="Status message from the database.")

# ─── MCP Toolset ─────────────────────────────────────────────────────────────

MCP_SERVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")

mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[MCP_SERVER_PATH],
        ),
    ),
)

# ─── Sub-Agents ──────────────────────────────────────────────────────────────

extraction_agent = LlmAgent(
    name="extraction_agent",
    model=_MODEL,
    **_CALLBACKS,
    instruction=(
        "You are a financial information extractor. The input may be in English, Bangla, Banglish, or mixed. "
        "Extract: date (YYYY-MM-DD, resolve relative dates like today/yesterday), type (Income or Expense), "
        "contact (person involved or None), category, details, amount, and due_status. "
        "Return structured output matching the ExtractedTransaction schema."
    ),
    description="Extracts structured financial details from natural language input.",
    output_schema=ExtractedTransaction,
)

validation_agent = LlmAgent(
    name="validation_agent",
    model=_MODEL,
    **_CALLBACKS,
    instruction=(
        "You are a financial transaction validator. "
        "Validate that: (1) amount is present and > 0, (2) type is 'Income' or 'Expense', "
        "(3) category and details are present. "
        "If any critical field is missing: set is_valid=False, list missing_fields, write a concise clarification_question. "
        "If all present: is_valid=True, missing_fields=[], clarification_question=None."
    ),
    description="Validates extracted transaction fields and formulates clarification questions.",
    output_schema=ValidationResult,
)

database_agent = LlmAgent(
    name="database_agent",
    model=_MODEL,
    **_CALLBACKS,
    instruction=(
        "You are the Database Agent. Use the save_transaction MCP tool to persist the transaction. "
        "Pass all fields: date, type, amount, category, details, contact, due_status. "
        "Return success status and message matching the DatabaseAgentOutput schema."
    ),
    description="Saves verified transactions to the database using MCP tools.",
    tools=[mcp_toolset],
    output_schema=DatabaseAgentOutput,
)

orchestrator_agent = LlmAgent(
    name="orchestrator_agent",
    model=_MODEL,
    **_CALLBACKS,
    instruction=(
        "You are the Finance Memory Orchestrator. Follow this sequence:\n"
        "1. Call extraction_agent to extract transaction fields from the user message.\n"
        "2. Call validation_agent to validate the extracted fields.\n"
        "3. If the validation says all fields are valid, output exactly:\n"
        '   {"is_valid":true,"transaction":{...},"clarification_question":null}\n'
        "   where transaction contains date, type, amount, category, details, contact, due_status.\n"
        "4. If the validation says fields are missing, output exactly:\n"
        '   {"is_valid":false,"transaction":null,"clarification_question":"the question"}\n'
        "Output ONLY the JSON object. Do NOT use `print` or any other function. "
        "Do NOT include any other text, markdown, or code fences."
    ),
    tools=[AgentTool(extraction_agent), AgentTool(validation_agent)],
)

# ─── Helpers ───────────────────────────────────────────────────────────────────

def _text_from(content: Any) -> str:
    if hasattr(content, "parts") and content.parts:
        return content.parts[0].text or ""
    return str(content) if content else ""

def _try_parse_json(text: str) -> dict | None:
    text = text.strip()
    # Strip leading non-JSON garbage (e.g. <ctrl42>call\nprint(...))
    brace = text.find("{")
    if brace >= 0:
        text = text[brace:]
    # Strip trailing non-JSON garbage
    brace_end = text.rfind("}")
    if brace_end >= 0:
        text = text[: brace_end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None

# ─── Workflow Function Nodes ──────────────────────────────────────────────────

async def security_checkpoint(ctx: Context, node_input: Any) -> Event:
    query = _text_from(node_input)
    ctx.state["original_query"] = query

    # 1. PII scrubbing (must run before injection check to prevent PII leakage)
    scrubbed = query
    if config.pii_redaction_enabled:
        scrubbed = re.sub(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', "[EMAIL_REDACTED]", query)
        scrubbed = re.sub(r'\+?\d{10,12}', "[PHONE_REDACTED]", scrubbed)
        if scrubbed != query:
            logger.info(json.dumps({"event": "pii_scrubbed", "severity": "INFO"}))

    # 2. Prompt injection check (on scrubbed content)
    if config.injection_detection_enabled:
        injection_keywords = [
            "ignore instructions", "system prompt", "delete database",
            "bypass", "override", "you are now",
        ]
        hits = [kw for kw in injection_keywords if kw in scrubbed.lower()]
        if hits:
            logger.warning(json.dumps({"event": "security_violation", "severity": "CRITICAL", "keywords": hits}))
            ctx.state["security_error"] = "Security Checkpoint Blocked: Potential prompt injection detected."
            return Event(output=scrubbed, route="SECURITY_EVENT")

    # 3. Domain rule: single-transaction amount limit 1,000,000 Taka
    for num_str in re.findall(r'\b\d+\b', scrubbed):
        if int(num_str) > 1_000_000:
            logger.warning(json.dumps({"event": "domain_anomaly", "severity": "WARNING", "value": num_str}))
            ctx.state["security_error"] = f"Security Checkpoint Blocked: Amount {num_str} exceeds single-transaction limit of 1,000,000 Taka."
            return Event(output=scrubbed, route="SECURITY_EVENT")

    ctx.state["original_query"] = scrubbed
    logger.info(json.dumps({"event": "security_check_passed", "severity": "INFO"}))
    return Event(output=scrubbed, route="clean")

async def _check_completeness_impl(ctx: Context, node_input: Any) -> AsyncGenerator[Event | RequestInput, None]:
    # If resuming after HITL clarification — rerun_on_resume=True means ctx.resume_inputs is available
    if ctx.resume_inputs and "clarification" in ctx.resume_inputs:
        clarification = ctx.resume_inputs["clarification"]
        original = ctx.state.get("original_query", "")
        new_query = f"{original}. Additional info: {clarification}"
        ctx.state["original_query"] = new_query
        logger.info(json.dumps({"event": "hitl_resume", "severity": "INFO", "clarification": clarification}))
        yield Event(output=new_query, route="retry")
        return

    text = _text_from(node_input)
    data = _try_parse_json(text) or {}

    is_valid = data.get("is_valid", False)
    transaction = data.get("transaction")
    question = data.get("clarification_question")

    if not is_valid or not transaction:
        msg = question or "Could you provide more details about this transaction?"
        yield RequestInput(interrupt_id="clarification", message=msg)
        return

    logger.info(json.dumps({"event": "transaction_validated", "severity": "INFO"}))
    tx_text = (
        f"Save this transaction: date={transaction.get('date')}, "
        f"type={transaction.get('type')}, amount={transaction.get('amount')}, "
        f"category={transaction.get('category')}, details={transaction.get('details')}, "
        f"contact={transaction.get('contact')}, due_status={transaction.get('due_status')}"
    )
    yield Event(output=tx_text, route="save")

# Wrap with rerun_on_resume=True so ctx.resume_inputs is populated on HITL resume
check_completeness = FunctionNode(func=_check_completeness_impl, rerun_on_resume=True)

async def security_error(ctx: Context, node_input: Any) -> AsyncGenerator[Event, None]:
    msg = ctx.state.get("security_error", "Security check failed.")
    yield Event(
        content=types.Content(role="model", parts=[types.Part.from_text(text=msg)]),
        output={"error": msg},
    )

async def final_output(ctx: Context, node_input: dict) -> AsyncGenerator[Event, None]:
    if "error" in node_input:
        yield Event(
            content=types.Content(role="model", parts=[types.Part.from_text(text=node_input["error"])]),
            output=node_input,
        )
        return

    success = node_input.get("success", False)
    db_msg = node_input.get("message", "")
    text = f"✅ Transaction Recorded!\n\n{db_msg}" if success else f"❌ Failed to save:\n\n{db_msg}"
    yield Event(
        content=types.Content(role="model", parts=[types.Part.from_text(text=text)]),
        output=node_input,
    )

# ─── Workflow Graph ───────────────────────────────────────────────────────────

root_agent = Workflow(
    name="ai_finance_memory",
    edges=[
        # Entry → security gate
        (START, security_checkpoint),

        # Security gate branches via RoutingMap
        (security_checkpoint, {"SECURITY_EVENT": security_error, "clean": orchestrator_agent}),

        # Orchestrator always flows to completeness check
        (orchestrator_agent, check_completeness),

        # Completeness check: retry loops back; save proceeds to DB
        (check_completeness, {"retry": orchestrator_agent, "save": database_agent}),

        # Terminal nodes both flow to final_output
        (database_agent, final_output),
        (security_error, final_output),
    ],
    description="Secure conversational personal finance recording agent.",
)

app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True),
)
