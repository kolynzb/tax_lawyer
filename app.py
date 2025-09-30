import os, httpx, asyncio,logging, chainlit as cl
from typing import Dict, Optional
from google import genai
import re

AUTO_GROUND_PATTERNS = (
    r"\b(latest|current|this year|updated|new)\b",
    r"\b(rate|threshold|penalt(y|ies)|deadline|due date|gazette|practice note)\b",
    r"\b(VAT|PAYE|WHT|Excise|EFRIS)\b",
    r"\b20(2[3-9]|3[0-9])\b",
)

def should_suggest_web(q: str) -> bool:
    text = q.lower()
    return any(re.search(p, text) for p in AUTO_GROUND_PATTERNS)

logger = logging.getLogger(__name__)

# Initialize Gemini client with API key
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

SYSTEM_PROMPT = (
    "You are 'Tax Lawyer (Uganda)', an assistant for tax accountants and lawyers in Uganda. "
    "Always: (1) state assumptions, (2) ask for missing facts, (3) prefer Ugandan tax law sources "
    "(Income Tax Act, VAT Act, Tax Procedures Code, practice notes), (4) cite section numbers "
    "when you know them, (5) flag uncertainty and advise verification, (6) avoid definitive legal conclusions."
    "Make answers concise but informative, and use bullet points or numbered lists for clarity, unless the user prefers a different style."
)

MODEL_NAME = "gemini-2.0-flash"  # or gemini-1.5-flash for faster/lower-cost replies

UG_FILTER = (
    "site:ura.go.ug OR site:finance.go.ug OR site:parliament.go.ug"
    "OR site:law.africa OR site:ulii.org OR site:gazettes.africa"
    "OR site:ulrc.go.ug OR site:ugandalaws.com OR site:ulrc.go.ug"
)

async def brave_search(query: str, k: int = 6):
    """
    Brave Search API → returns [{title, url, snippet}, ...]
    Note: omit unsupported params (e.g., country="UG") to avoid 422.
    """
    key = os.environ["BRAVE_API_KEY"]
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": key,
        # Optional but sometimes helpful:
        "User-Agent": "TaxLawyerUg/1.0"
    }
    params = {
        "q": query,         # keep your UG_FILTER concatenated in the caller
        "count": k,
        # "search_lang": "en",        # optional
        # "safesearch": "moderate",   # optional
        # "freshness": "month"        # optional
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers=headers,
            params=params
        )
        # If Brave returns 422 or other 4xx/5xx, show the body to help debug
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            body = r.text[:500]
            raise RuntimeError(f"Brave search error {r.status_code}: {body}") from None

        data = r.json()

    out = []
    for item in data.get("web", {}).get("results", []):
        out.append({
            "title": item.get("title"),
            "url": item.get("url"),
            "snippet": item.get("description", "")
        })
    return out

# Composer commands (icons inside the input bar)
COMMANDS = [
    {"id": "WebSearch",   "icon": "search",   "description": "Search the Web (Brave)"},
    {"id": "GroundToggle", "icon": "globe",   "description": "Always ground this thread"},
]


@cl.oauth_callback
def oauth_login(provider_id: str, token: str, raw_user_data: Dict[str, str], default_user: cl.User) -> Optional[cl.User]:
    if provider_id != "google":
        return None
    allowed_domain = os.getenv("GOOGLE_ALLOWED_DOMAIN")
    if allowed_domain and raw_user_data.get("hd") != allowed_domain:
        return None
    email = (raw_user_data.get("email") or default_user.identifier).lower()
    return cl.User(identifier=email, metadata={
        "provider": "google",
        "name": raw_user_data.get("name"),
        "picture": raw_user_data.get("picture"),
    })


@cl.set_starters
async def set_starters():
    return [
         cl.Starter(label="🔎 Search the Web", message="web: ", icon="/public/search.svg"),
        cl.Starter(
            label="VAT threshold in Uganda",
            message="What is the VAT threshold in Uganda?",
            icon="/public/idea.svg",
        ),
        cl.Starter(
            label="Register for EFRIS",
            message="How do I register for EFRIS?",
            icon="/public/learn.svg",
        ),
        cl.Starter(
            label="PAYE tax rates",
            message="What are the PAYE tax rates?",
            icon="/public/terminal.svg",
        ),
        cl.Starter(
            label="Documents for tax registration",
            message="What documents are needed for tax registration?",
            icon="/public/write.svg",
        ),
        cl.Starter(
            label="Penalties for late tax filing",
            message="What are the penalties for late tax filing?",
            icon="/public/alert.svg",
        ),
        cl.Starter(
            label="Withholding tax in Uganda",
            message="How does withholding tax work in Uganda?",
            icon="/public/info.svg",
        ),
    ]

@cl.on_chat_start
async def start():
    """Welcome message when chat starts"""
    user = cl.user_session.get("user")
    await cl.context.emitter.set_commands(COMMANDS) 
    logger.info(f"{user.identifier} has started the conversation")

    # await cl.Message(content="Hello! I'm your Uganda Tax AI assistant. How can I help you today?").send()

@cl.on_chat_resume
async def on_resume(thread):
    # thread contains persisted steps and messages
    await cl.Message(content="↩️ Resumed previous conversation.").send()

    # Optional: rebuild the conversation context for Gemini
    # (e.g. feed prior messages as history for better continuity)
    history = []
    for step in thread["steps"]:
        # Only grab user and assistant steps (ignore tool/metadata-only ones if you want)
        if step.get("type") == "message":
            role = "user" if step.get("author") == cl.user_session.get("user").identifier else "model"
            history.append({"role": role, "parts": [step.get("output") or step.get("input", "")]})

    # Store in user session so your @on_message can prepend history
    cl.user_session.set("gemini_history", history)


@cl.action_callback("composer_web")
async def _composer_web(action: cl.Action):
    # Prefill the composer so they just type
    await cl.Message(content="web: ").send()

@cl.on_message
async def on_message(message: cl.Message):
    q_raw = (message.content or "").strip()
    history = cl.user_session.get("gemini_history") or []

    # Handle composer commands
    if message.command == "GroundToggle":
        cur = bool(cl.user_session.get("ground_always"))
        cl.user_session.set("ground_always", not cur)
        await cl.Message(content=f"Thread grounding **{'enabled' if not cur else 'disabled'}**.").send()
        return

    # Decide if this turn uses web grounding
    use_web = False
    q = q_raw
    if message.command == "WebSearch":
        use_web = True
    elif q_raw.lower().startswith("web:"):
        use_web = True
        q = q_raw[4:].strip()
    elif cl.user_session.get("ground_always"):
        use_web = True

    # ====== WEB SEARCH (Brave) when needed ======
    sources = []
    if use_web:
        query = f"{q} {UG_FILTER}"  # bias to authoritative UG sources
        async with cl.Step(name="websearch", type="tool", show_input=True) as step:
            sources = await brave_search(query, k=6)
            step.output = {"query": query, "sources": [s["url"] for s in sources]}
        if sources:
            bullets = "\n".join(
                f"- [{s['title']}]({s['url']}) — {s['snippet'][:160]}…"
                for s in sources
            )
            await cl.Message(content=f"**Sources (Brave):**\n{bullets}").send()

    # ====== Build grounded prompt for Gemini ======
    # Short conversational history (trimmed)
    hist_text = []
    for turn in history[-8:]:
        role = "User" if turn.get("role") == "user" else "Assistant"
        text = (turn.get("parts") or [""])[0]
        if text:
            hist_text.append(f"{role}: {text}")
    hist_text = "\n".join(hist_text)

    ctx_text = "\n\n".join(
        f"[{i+1}] {s['title']} — {s['url']}\n{s['snippet']}"
        for i, s in enumerate(sources)
    )

    prompt_to_model = (
        f"{SYSTEM_PROMPT}\n\n"
        f"{('Conversation so far:\\n' + hist_text + '\\n\\n') if hist_text else ''}"
        f"User question:\n{q}\n\n"
        f"{('Context from authoritative Uganda web sources:\\n' + ctx_text + '\\n\\n') if sources else ''}"
        "Instructions:\n"
        "- Answer for Uganda context; prefer statutes/official guidance.\n"
        "- Use bracket refs like [1], [2] that map to the numbered sources above.\n"
        "- If unsure, say what to verify.\n"
    )

    # ====== Stream Gemini (new SDK -> pass a string to contents) ======
    msg = cl.Message(content="")
    await msg.send()

    def _stream():
        return client.models.generate_content_stream(
            model=MODEL_NAME,
            contents=prompt_to_model,  # ✅ string, not list of dicts
            config={"system_instruction": SYSTEM_PROMPT, "temperature": 0.3, "max_output_tokens": 1200},
        )

    stream = await asyncio.get_event_loop().run_in_executor(None, _stream)

    async with cl.Step(name="gemini", type="tool", show_input=True, metadata={"web": use_web}) as step:
        parts = []
        for event in stream:
            if event.candidates and event.candidates[0].content.parts:
                t = event.candidates[0].content.parts[0].text
                parts.append(t)
                await msg.stream_token(t)
        step.output = {"chars": len("".join(parts))}
    await msg.update()

    # Persist history
    answer = "".join(parts)
    history.append({"role": "user", "parts": [q_raw]})
    history.append({"role": "model", "parts": [answer]})
    cl.user_session.set("gemini_history", history)

    # ====== Nudge: offer verification when we didn't ground but should ======
    if not use_web and should_suggest_web(q_raw):
        await cl.Message(
            content="Do you want me to **verify** that with authoritative Ugandan sources?",
            actions=[cl.Action(name="composer_web", label="🔎 Verify with Web")]
        ).send()