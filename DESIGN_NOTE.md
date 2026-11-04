# Design Note — Home-Service Booking Agent

## The Big Picture

Imagine you're texting a helpful friend who knows all the plumbers, electricians, and cleaners in your city. You tell them what you need, they find options, and they only book after you say "yes". That's this agent.

Under the hood, five key ideas make it work reliably:

---

## 1. State Model — "Where are we in the conversation?"

The agent is always in one of **two states**:

```
 ┌──────────┐    user picks provider + slot     ┌────────────────────┐
 │ CHATTING  │ ──────────────────────────────▶   │ AWAITING APPROVAL  │
 │           │ ◀──────────────────────────────   │                    │
 └──────────┘    user says yes/no               └────────────────────┘
```

- **Chatting** — the AI can search for providers, show details, answer questions. It has access to "safe" tools only (search, hold a slot, remember preferences). Nothing irreversible happens here.
- **Awaiting Approval** — the AI has proposed a booking/cancellation/reschedule. It **stops and waits**. The only thing that moves it forward is the user saying "yes" or "no".

This means the agent can never get confused about whether you've approved something — the state machine knows.

---

## 2. Approval Enforcement — "Never book without permission"

This is the most important safety feature. Here's the trick:

> **The AI never has access to the Book, Cancel, or Reschedule buttons.**

Those actions are locked away. The AI can only *propose* them. When you say "yes", the **application code** (not the AI) presses the button.

Think of it like a child asking a parent: "Can I buy this toy?" The child can browse and pick, but only the parent has the credit card.

```
AI can do:           AI CANNOT do:
✅ Search providers   ❌ Book a slot
✅ Hold a slot        ❌ Cancel a booking
✅ Propose a booking  ❌ Reschedule
✅ Show details       ❌ Spend your money
```

This is enforced by **LangGraph** — the AI literally doesn't have these tools in its toolbox. Even if it hallucinates or gets confused, it physically cannot book without your approval.

---

## 3. Idempotency & Recovery — "What if something goes wrong?"

Real systems fail — networks drop, servers crash, requests time out. The agent handles this using **idempotency keys** (fancy word for "don't do the same thing twice").

**How it works:**

Every booking/cancel/reschedule gets a unique ID (like a receipt number). If the request fails and we retry, we send the **same ID**. The server sees "oh, I already processed this" and doesn't double-book.

**Error scenarios and what happens:**

| What goes wrong | What the agent does |
|---|---|
| Booking times out | Checks if it actually went through before retrying |
| Slot taken by someone else | Tells you and offers other slots or providers |
| Hold expires | Gets a new hold and re-confirms with you |
| Cancel fails (server error) | Retries once (it works on the second try) |
| Reschedule slot conflict | Shows you alternative available slots |
| Price changed since you looked | Tells you the new price and asks for fresh approval |

The key idea: **never assume success or failure — always check**.

---

## 4. Memory — "Remembering what you like"

The agent remembers your preferences across sessions (even after you close and reopen it).

**Two types of memory:**

- **Explicit** — you directly said it: *"My area is Koramangala"*
- **Inferred** — the agent noticed a pattern: *"User always picks the cheapest option"*

**Where it's stored:** A simple JSON file on your computer (`data/memory.json`). Nothing goes to the cloud.

**How it's used:** Next time you say "I need a plumber", the agent already knows your area and preferred time — but it **always confirms** before assuming: *"Should I search in Koramangala with morning slots?"*

**You're in control:**
- *"Forget my preferred area"* → deleted
- *"What do you know about me?"* → shows everything stored
- *"Actually I moved to Whitefield"* → updates it

---

## 5. Observability — "What did the agent actually do?"

Every conversation is recorded in a **trace file** (like a flight recorder for the agent). This helps us debug issues and understand how the agent behaves.

**What gets logged:**

| Event | What it captures |
|---|---|
| User message | What you typed |
| LLM call | Which AI model was used, how long it took |
| Tool call | Which tool was called, what arguments, what result |
| Approval | What action was proposed, did you say yes or no |
| Agent response | What the agent said back |
| Errors | Hallucination detected, retries, timeouts |

**What you get at the end of each session:**

```
📊 Trace saved to data/traces/trace_20261104_001234.json
   Session: 120s | LLM calls: 8 | Tool calls: 4 | Messages: 5
```

This makes it easy to answer questions like:
- "Why did the agent suggest that provider?" → check which tool returned it
- "Why was the response slow?" → check LLM latency
- "Did it actually call the booking API?" → check tool call log

---

## Example Trace — What Happens When a Booking Times Out

Here's a real conversation where the booking request timed out, but the system recovered and found that it actually went through. The trace file (`data/traces/timeout_success_trace.json`) recorded every step automatically.

> **User:** "I need a plumber in Koramangala this weekend, budget 1000"

| Step | What happened | Time |
|------|--------------|------|
| 🔍 **Search** | Agent calls `search_providers(plumbing, Koramangala)` | 0s |
| | Found 2 providers: Raju Plumbing ₹800, Kumar & Sons ₹1200 | |

> **User:** "I want Raju Plumbing Services, Monday 11 AM slot"

| Step | What happened | Time |
|------|--------------|------|
| 🔒 **Hold** | Agent calls `hold_slot(PLB-001, Monday 11:00 AM)` → slot reserved | +72s |
| 📋 **Propose** | Agent calls `propose_action(book)` → shows details, waits for user | +83s |

> **User:** "Yes"

| Step | What happened | Time |
|------|--------------|------|
| ✅ **Approval** | System detects confirmation, state = `awaiting_approval` | +112s |
| 📞 **Book** | System (not AI) calls `book(hold_id, idempotency_key)` | +112s |
| ⚠️ **Timeout!** | Server didn't respond in time — did it go through or not? | +112s |
| 🔄 **Auto-check** | System calls `get_booking_status(hold_id, idempotency_key)` | +112s |
| ✅ **Confirmed** | Booking actually went through! Same idempotency key = no duplicate | +112s |

> **Agent:** "⏳ The booking request timed out. Let me check the status... ✅ Your booking went through!"

**What this trace shows:**

- The AI never touched the Book button — the system code did it after user said "Yes"
- When the booking timed out, the system didn't panic or retry blindly — it checked the status first
- The idempotency key ensured that even if we had retried, no duplicate booking would be created
- All 34 events were logged automatically with timestamps for debugging

---

## Summary

| Concept | One-line explanation |
|---|---|
| State Model | Agent is either chatting or waiting for your yes/no — never confused |
| Approval Enforcement | AI proposes, only your code executes — AI can't book by itself |
| Idempotency & Recovery | Unique IDs prevent double-booking; every error has a recovery path |
| Memory | Remembers your preferences in a local file; you can view/edit/delete anytime |
| Observability | Every action is logged with timestamps so you can debug any conversation |
