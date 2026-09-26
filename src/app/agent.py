import asyncio
from datetime import datetime
import os
import sys
import logging
import json

from app.whatsapp import send_message
from shared import db
from shared.models import Patient
from shared.message_utils import extract_reply_text

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_mcp_adapters.prompts import load_mcp_prompt

from langgraph.graph import MessagesState
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from langchain_anthropic import ChatAnthropic

logger = logging.getLogger(__name__)


class State(MessagesState):
    """Extends the base MessagesState to include last interaction time and patient info."""
    last_interaction_time: datetime | None = None
    patient: Patient | None = None
    conversation_id: int | None = None


class ClinicWorker:
    """
    Runs the whole message-handling pipeline for ONE clinic: its own MCP
    tool subprocess (with that clinic's own WhatsApp credentials and config
    file in its environment), its own LangGraph agent, its own message
    queue, and its own database file.

    Multiple ClinicWorkers run side by side inside the same FastAPI
    process (one per clinic, see app/main.py's startup), which is what
    lets one Mawaid deployment serve several clinics at once without a
    separate Railway service per clinic. Each worker's database calls run
    inside its own asyncio task, with shared/db.py's contextvar pointed at
    that clinic's own database file (see db.set_current_db_path) -- so two
    clinics' data can never mix, even though the code is shared.
    """

    def __init__(self, clinic: dict):
        self.clinic = clinic
        self.message_queue: asyncio.Queue = asyncio.Queue()
        self.agent = None

        clinic_dir = os.path.dirname(clinic["config_path"])
        server_env = os.environ.copy()
        server_env.update({
            "META_ACCESS_TOKEN": clinic["whatsapp_access_token"],
            "META_PHONE_NUMBER_ID": clinic["whatsapp_phone_number_id"],
            "CLINIC_CONFIG_PATH": clinic["config_path"],
            "CLINIC_DB_PATH": clinic["db_path"],
            "CLINIC_STATIC_URL_PREFIX": f"/clinic-static/{clinic['slug']}/static",
        })

        self.server_params = StdioServerParameters(
            # Launch the MCP server as a module directly, using the exact
            # same Python interpreter/environment this process is already
            # running under (sys.executable). Each clinic gets its OWN
            # subprocess with its OWN environment above -- that's what
            # keeps clinic_config.py and whatsapp.py (which just read
            # os.environ, unchanged) correct without needing to know
            # anything about multi-clinic support themselves.
            command=sys.executable,
            args=["-m", "dentaldesk_mcp", "--verbose"],
            env=server_env,
            cwd=os.getcwd(),
        )

    async def create_graph(self, session):
        """
        Creates and returns this clinic's agent graph. Same shape as the
        original single-clinic version -- the system prompt is generic
        (Sia looks up clinic-specific facts via tools rather than having
        them baked into her instructions), so it doesn't need to change
        per clinic; only the tools/session underneath it do.
        """
        logger.info("Creating agent graph for clinic %s (%s)...", self.clinic["id"], self.clinic["name"])

        tools = await load_mcp_tools(session)
        systemPrompt = await load_mcp_prompt(session, "system_prompt")

        llm = ChatAnthropic(model=os.getenv("ANTHROPIC_MODEL_NAME") or "claude-sonnet-5")
        llm_with_tools = llm.bind_tools(tools)

        builder = StateGraph(State)

        def assistant(state: State):
            """The main assistant node that generates responses using the LLM and tools."""
            logger.debug(f"Assistant node invoked with state: {state}")
            patient = state.get("patient")
            patient_context = ""
            if patient:
                patient_context = (f"\n\n--- Current Patient Information ---\n"
                                 f"Name: {patient.name}\n"
                                 f"Age: {patient.age or 'Not provided'}\n"
                                 f"Gender: {patient.gender or 'Not provided'}\n"
                                 f"Phone Number: {patient.phone_number}\n"
                                 f"Current Conversation ID: {state.get('conversation_id')}\n"
                                 f"---------------------------------")

            final_system_prompt = systemPrompt[0].content + patient_context
            sys_msg = SystemMessage(content=final_system_prompt)
            response = llm_with_tools.invoke([sys_msg] + state["messages"])
            return {"messages": [response]}

        def update_timestamp_node(state: State) -> dict:
            """Nodes that just updates the timestamp in the state."""
            return {"last_interaction_time": datetime.now()}

        builder.add_node("assistant", assistant)
        builder.add_node("tools", ToolNode(tools))
        builder.add_node("update_timestamp", update_timestamp_node)

        builder.add_edge(START, "assistant")
        builder.add_conditional_edges(
            "assistant",
            tools_condition,
            {
                "tools": "tools",
                "__end__": "update_timestamp"
            }
        )
        builder.add_edge("tools", "assistant")
        builder.add_edge("update_timestamp", END)

        memory = InMemorySaver()
        agent = builder.compile(checkpointer=memory)

        return agent

    async def enqueue_message(self, patient_phone: str, message: str):
        """
        Enqueues an incoming message for processing by THIS clinic's
        agent. Finds or creates a patient and a conversation (in this
        clinic's own database), then adds the message to this clinic's
        own queue.
        """
        logger.debug(f"[{self.clinic['slug']}] Enqueueing message from {patient_phone}")

        patient = db.get_patient_by_phone(patient_phone)
        if not patient:
            patient = db.create_patient(Patient(name="New Patient", phone_number=patient_phone))

        conversation = db.get_open_conversation(patient.id)
        if not conversation:
            conversation = db.create_conversation(patient.id)

        logger.info(f"[{self.clinic['slug']}] Enqueuing message for patient {patient.id} in conversation {conversation.id}")

        await self.message_queue.put({
            "conversation_id": conversation.id,
            "patient": patient,
            "message": message,
            "timestamp": datetime.now(),
        })

    async def consume_messages(self):
        """
        Continuously consumes messages from this clinic's queue and
        processes them with this clinic's agent. Identical logic to the
        original single-clinic consumer, just scoped to one clinic's queue,
        agent, and WhatsApp credentials.
        """
        agent = self.agent
        logger.info(f"[{self.clinic['slug']}] Starting message consumer...")

        while True:
            logger.debug(f"[{self.clinic['slug']}] Waiting for new message in queue...")
            task = await self.message_queue.get()
            conversation_id = task.get("conversation_id")
            try:
                patient = task["patient"]
                message = task["message"]
                patient_phone_for_reply = patient.phone_number

                config = {"configurable": {"thread_id": str(conversation_id)}}

                current_state = await agent.aget_state(config)
                logger.debug(f"[{self.clinic['slug']}] Current state for conversation {conversation_id}: {current_state}")

                if current_state is None or not current_state.values.get("messages"):
                    logger.info(f"[{self.clinic['slug']}] No agent state found for conversation {conversation_id}. Checking DB for history...")
                    history = db.get_messages(conversation_id)
                    history_messages = []
                    if history:
                        for msg in history:
                            if msg.sender == 'user':
                                history_messages.append(HumanMessage(content=msg.message))
                            elif msg.sender == 'agent':
                                history_messages.append(AIMessage(content=msg.message))
                            elif msg.sender == 'agent_tool_call':
                                tool_calls = json.loads(msg.message)
                                history_messages.append(AIMessage(content="", tool_calls=tool_calls))
                            elif msg.sender == 'tool':
                                tool_data = json.loads(msg.message)
                                history_messages.append(ToolMessage(content=tool_data['content'], tool_call_id=tool_data['tool_call_id']))

                    update_payload = {
                        "messages": history_messages,
                        "patient": patient,
                        "conversation_id": conversation_id,
                    }
                    logger.info(f"[{self.clinic['slug']}] Updating state for conversation {conversation_id} with {len(history_messages)} messages and patient info.")
                    await agent.aupdate_state(config, update_payload, START)

                db.add_message(conversation_id=conversation_id, sender="user", message=message)

                response = await agent.ainvoke({"messages": [HumanMessage(content=message)]}, config)

                last_human_message_index = -1
                for i, msg in reversed(list(enumerate(response["messages"]))):
                    if isinstance(msg, HumanMessage):
                        last_human_message_index = i
                        break

                if last_human_message_index != -1:
                    messages_this_turn = response["messages"][last_human_message_index + 1:]
                    for msg in messages_this_turn:
                        if isinstance(msg, AIMessage):
                            # A reply can carry text for the patient AND a tool
                            # call in the same message ("let me check that for
                            # you" + a slot lookup). The text used to be dropped
                            # whenever a tool call was present -- never sent,
                            # and missing from the dashboard transcript.
                            reply_text = extract_reply_text(msg.content) if msg.content else ""
                            if reply_text:
                                db.add_message(conversation_id=conversation_id, sender="agent", message=reply_text)
                                self.send(patient_phone_for_reply, reply_text)
                            if msg.tool_calls:
                                db.add_message(
                                    conversation_id=conversation_id,
                                    sender="agent_tool_call",
                                    message=json.dumps(msg.tool_calls)
                                )
                        elif isinstance(msg, ToolMessage):
                            tool_data = {"content": msg.content, "tool_call_id": msg.tool_call_id}
                            db.add_message(
                                conversation_id=conversation_id,
                                sender="tool",
                                message=json.dumps(tool_data)
                            )
                else:
                    reply = extract_reply_text(response["messages"][-1].content)
                    if reply:
                        db.add_message(conversation_id=conversation_id, sender="agent", message=reply)
                        self.send(patient_phone_for_reply, reply)

            except Exception as e:
                logger.error(f"[{self.clinic['slug']}] [Agent Error] Failed to process message for conversation {conversation_id}: {e}", exc_info=True)

            finally:
                self.message_queue.task_done()

    def send(self, phone_number: str, message: str):
        """Sends a WhatsApp text message using THIS clinic's own
        credentials -- never the process-wide environment variables, since
        several clinics' credentials all coexist in this one process now."""
        send_message(
            phone_number,
            message,
            access_token=self.clinic["whatsapp_access_token"],
            phone_number_id=self.clinic["whatsapp_phone_number_id"],
            api_version=os.environ.get("GRAPH_API_VERSION"),
        )

    async def conversation_cleanup_task(self):
        """
        Periodically checks for timed-out conversations (for this clinic
        only) and closes them. Identical logic to the original
        single-clinic version, just scoped to this clinic's agent/db.
        """
        logger.info(f"[{self.clinic['slug']}] Starting conversation cleanup task...")
        interval_minutes = int(os.getenv("CONVERSATION_TIMEOUT_MINUTES", 30))
        agent = self.agent

        while True:
            await asyncio.sleep(interval_minutes * 60)
            logger.info(f"[{self.clinic['slug']}] Running conversation cleanup task...")

            checkpointer = agent.checkpointer
            if checkpointer is None:
                logger.warning(f"[{self.clinic['slug']}] Agent has no checkpointer. Skipping cleanup.")
                continue

            all_thread_ids = set()
            try:
                for item in checkpointer.list(None):
                    config = item.config
                    thread_id = config["configurable"]["thread_id"]
                    all_thread_ids.add(thread_id)
            except Exception as e:
                logger.error(f"[{self.clinic['slug']}] Error listing threads from checkpointer: {e}", exc_info=True)

            if not all_thread_ids:
                logger.info(f"[{self.clinic['slug']}] No active conversation threads found in checkpointer.")
                continue

            try:
                logger.debug(f"[{self.clinic['slug']}] Current active threads in checkpointer: {all_thread_ids}")
                for thread_id in all_thread_ids:
                    config = {"configurable": {"thread_id": thread_id}}
                    state = await agent.aget_state(config)
                    logger.debug(f"[{self.clinic['slug']}] Checking thread {thread_id} with state: {state}")
                    if state and state.values and state.values.get("last_interaction_time"):
                        last_interaction = state.values["last_interaction_time"]
                        if isinstance(last_interaction, str):
                            last_interaction = datetime.fromisoformat(last_interaction)

                        if (datetime.now() - last_interaction).total_seconds() > (interval_minutes * 60):
                            logger.info(f"[{self.clinic['slug']}] Conversation thread {thread_id} has timed out.")
                            conversation = db.get_conversation(int(thread_id))
                            if conversation and conversation.status == 'open':
                                logger.info(f"[{self.clinic['slug']}] Closing conversation {thread_id} in DB with reason 'timed_out'.")
                                db.close_conversation(int(thread_id), reason="timed_out")
                            else:
                                logger.info(f"[{self.clinic['slug']}] Conversation {thread_id} already closed in DB, skipping DB update.")

                            checkpointer.delete_thread(thread_id)
                            logger.info(f"[{self.clinic['slug']}] Removed thread {thread_id} from agent checkpointer.")

                    elif not state or not state.values:
                        checkpointer.delete_thread(thread_id)
                        logger.info(f"[{self.clinic['slug']}] Found lingering empty thread {thread_id}. Cleaning up.")

            except Exception as e:
                logger.error(f"[{self.clinic['slug']}] Error during cleanup for thread {thread_id}: {e}", exc_info=True)

    async def run(self):
        """
        Brings this one clinic fully online: points shared/db.py at this
        clinic's own database file for the lifetime of this task, starts
        its MCP subprocess, builds its agent, and runs its queue consumer
        and cleanup task forever (until the app shuts down).
        """
        db.set_current_db_path(self.clinic["db_path"])
        logger.info(f"[{self.clinic['slug']}] Checking and initializing database...")
        db.init_db(seed=True, dentists=self.clinic.get("dentists"))

        async with stdio_client(self.server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await load_mcp_tools(session)
                logger.debug(f"[{self.clinic['slug']}] Loaded MCP tools: {[tool.name for tool in tools]}")

                self.agent = await self.create_graph(session)

                asyncio.create_task(self.conversation_cleanup_task())

                await self.consume_messages()
