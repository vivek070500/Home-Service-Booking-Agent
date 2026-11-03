# 🏠 Home-Service Booking Agent

A chat-based AI agent that helps you find and book home services (plumbers, electricians, AC repair, cleaning, carpentry, painting) in Indian cities.

You talk to it like a normal person, it searches for providers, shows you options, and books the one you pick — only after you say "yes".

---

## How to Run (Step by Step)

### Step 1: Install Ollama

Ollama lets you run AI models on your own computer (free, no API key needed).

1. Go to [https://ollama.com](https://ollama.com)
2. Click **Download** and install it for your OS (Mac/Windows/Linux)

### Step 2: Download the AI Model

Open a terminal and run:

```bash
ollama pull qwen2.5:14b
```

This downloads the AI model (~9 GB). Wait for it to finish.

### Step 3: Start Ollama

In a terminal, run:

```bash
ollama serve
```

**Keep this terminal open** — it runs the AI in the background.

### Step 4: Install Python Dependencies

Open a **new** terminal, go to the project folder, and run:

```bash
pip install -r requirements.txt
```

### Step 5: Run the Agent

In the same terminal:

```bash
python main.py
```

That's it! You'll see a chat prompt. Try typing:

```
I need a plumber in Koramangala this weekend, budget around 500
```

The agent will search for plumbers, show you options, and walk you through booking.

---

## How It Works

1. **You type** what service you need (e.g. "I need an electrician tomorrow")
2. **Agent searches** for providers in your area and shows a list with prices and ratings
3. **You pick** a provider and time slot
4. **Agent asks** "Do you want to confirm this booking?"
5. **You say yes** → Booking confirmed ✅

The agent **never books anything without your permission**. It always asks first.

---

## What You Can Say

| What you want | Example message |
|---|---|
| Find a service | "I need a plumber in Indiranagar" |
| Set preferences | "My area is Koramangala, I prefer mornings" |
| Pick a provider | "First one looks good" |
| Pick a time slot | "Monday 11 AM" |
| Confirm booking | "Yes" or "Confirm" |
| Reject booking | "No" or "Actually, find someone cheaper" |
| Cancel a booking | "Cancel my booking BKG-xxxxx" |
| See your bookings | "Show my bookings" |
| Quit | "quit" |

---

## Project Structure

```
home-service-agent/
├── main.py              ← Run this to start chatting
├── agent/
│   ├── graph_engine.py  ← The brain — LangGraph-based agent
│   ├── prompts.py       ← System prompt (instructions for the AI)
│   ├── memory.py        ← Remembers your preferences across sessions
│   ├── tracer.py        ← Logs what the agent does (for debugging)
│   ├── engine.py        ← Original engine (used by unit tests)
│   ├── schemas.py       ← Data models
│   └── state.py         ← Agent state definitions
├── mock_tools/
│   ├── tools.py         ← Simulated booking system (fake providers, slots, etc.)
│   └── config.py        ← Configuration for mock scenarios
├── tests/
│   ├── test_*.py        ← 170+ unit tests
│   ├── run_eval.py      ← E2E evaluation suite (25 scenarios)
│   └── gen_eval_table.py← Generates evaluation results table
├── requirements.txt     ← Python dependencies
└── .env.example         ← Example environment file
```

---

## Running Tests

```bash
# Run all unit tests
pytest tests/ -v

# Run a specific test file
pytest tests/test_mock_tools.py -v
```

---

## Options

```bash
# Use a different model
python main.py --model llama3.1:8b

# Use a different random seed (changes which mock errors occur)
python main.py --seed 123

# Disable memory (won't remember preferences)
python main.py --no-memory
```

---

## Notes

- The booking system is **simulated** (mock providers, not real ones) — it's a demo/prototype
- The AI model runs **locally on your computer** — nothing is sent to the cloud
- Works best with `qwen2.5:14b` or larger models
- Smaller models (like `llama3.1:8b`) work but may give worse results
