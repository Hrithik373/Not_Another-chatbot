# Track A — Auto-Remediation from Logs

Lyzr Agent Studio take-home submission. Quick links below; the engineering
detail (setup, run commands, pipeline design, reproducible numbers) lives in
[`harness/README.md`](harness/README.md).

## Links

| | |
|---|---|
| 🔴 **Live demo** | [not-another-chatbot.onrender.com](https://not-another-chatbot.onrender.com/) — Classify / Chat / Batch views, running against the real agent |
| 🤖 **Lyzr Studio agent** | [studio.lyzr.ai/.../6a7daa555f9f8a75ac0232b8](https://studio.lyzr.ai/create-new-agent/6a7daa555f9f8a75ac0232b8?tab=playground&public=true) |
| 💻 **Full codebase** | [github.com/Hrithik373/Not_Another-chatbot](https://github.com/Hrithik373/Not_Another-chatbot) |
| 📄 **Scoping note** (Part 1, 1 page) | [PDF](https://github.com/Hrithik373/Not_Another-chatbot/blob/main/harness/docs/scoping_note.pdf) · [Markdown source](harness/docs/scoping_note.md) |
| 📄 **Optimization write-up + results table** (Part 2) | [PDF](https://github.com/Hrithik373/Not_Another-chatbot/blob/main/harness/docs/optimization_writeup.pdf) · [Markdown source](harness/docs/optimization_writeup.md) |
| 📘 **Full README** (setup, pipeline, reviewer notes) | [`harness/README.md`](harness/README.md) |

## Demo run

```bash
cd harness
pip install -r requirements.txt
python run.py --mock        # offline pipeline smoke test, no credentials needed
python run.py                # real run against the Lyzr API (needs .env — see harness/.env.example)
python run.py --ablation     # per-lever cost/accuracy deltas
uvicorn server:app --reload  # serve the console locally
```

Full details, design rationale, and reviewer notes (token accounting,
pricing assumptions, mock-mode caveats) are in
[`harness/README.md`](harness/README.md).
