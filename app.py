import os
import json
import random
import time
import threading
from datetime import datetime
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# =========================
# ✅ CONFIG (FILL THESE)
# =========================
ACCESS_TOKEN = "EAAsExJjz63UBQ4m6QZCXZCkxI1gZAheqxFyyysqY99UzfRNLBFkmiI7WOKHSRb2z9D9FE9Udp8GDvVUISf13E6zuYckDXpw8FOgA8H56tluvDkBFpIu0kW5T0HT4OUVsqesRLKQlwHht6IaAVtTN5JrwbDD6zVeZCj1tEQdVs8HXn47ybq5VnHZB0ecjZCLEE7jQZDZD"
PHONE_NUMBER_ID = "964323003436477"
ADMIN_NUMBER = "919699276593"       # e.g. "919876543210" (no +)
VERIFY_TOKEN = "quizbot123"

# =========================
# ✅ GAME SETTINGS
# =========================
MIN_PLAYERS = 2          # you set 2 for testing, later make 5
QUESTIONS_PER_GAME = 3
QUESTION_TIME_LIMIT = 15  # seconds per question (auto next)
POINTS_PER_CORRECT = 1

# =========================
# ✅ GLOBAL STATE (in-memory)
# =========================
lock = threading.Lock()

players = set()          # phone numbers who joined current lobby
scores = {}              # {phone: score}
answer_time_sum = {}     # {phone: total_seconds_for_correct_answers} (tie-breaker)
joined_at = {}           # {phone: timestamp}

game = {
    "active": False,
    "questions": [],
    "q_index": -1,
    "question_open": False,
    "q_start_time": None,
    "answered": set(),        # who answered current question
    "fastest_correct": None,  # (phone, seconds)
    "timer": None
}

# =========================
# ✅ LOAD QUESTION POOL (Option A)
# =========================
def load_question_pool():
    path = os.path.join(os.path.dirname(__file__), "questions.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

ALL_QUESTIONS = load_question_pool()

# =========================
# ✅ WHATSAPP SEND
# =========================
def send_message(to_number: str, message: str):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    print("SEND STATUS:", r.status_code, r.text)

def broadcast(message: str, to_list=None):
    if to_list is None:
        to_list = list(players)
    for p in to_list:
        send_message(p, message)

# =========================
# ✅ UTILS
# =========================
def mask_number(num: str) -> str:
    # Example: 919876543210 -> 91****3210
    if not num or len(num) < 6:
        return num
    return num[:2] + "****" + num[-4:]

def top_leaderboard(n=5):
    # Sort by score desc, then time asc
    items = []
    for p in players:
        sc = scores.get(p, 0)
        t = answer_time_sum.get(p, 10**9)  # if no correct answers -> very large
        items.append((p, sc, t))
    items.sort(key=lambda x: (-x[1], x[2]))
    return items[:n]

def format_leaderboard(title="🏆 Leaderboard (Top 5)"):
    top = top_leaderboard(5)
    lines = [title]
    if not top:
        lines.append("No players yet.")
        return "\n".join(lines)
    for i, (p, sc, t) in enumerate(top, start=1):
        time_str = "-" if t >= 10**9 else f"{t:.1f}s"
        lines.append(f"{i}) {mask_number(p)} — {sc} pts — {time_str}")
    return "\n".join(lines)

# =========================
# ✅ GAME CORE
# =========================
def reset_lobby_and_game():
    global players, scores, answer_time_sum, joined_at
    with lock:
        # cancel timer if running
        if game["timer"]:
            try:
                game["timer"].cancel()
            except:
                pass
        game["active"] = False
        game["questions"] = []
        game["q_index"] = -1
        game["question_open"] = False
        game["q_start_time"] = None
        game["answered"] = set()
        game["fastest_correct"] = None
        game["timer"] = None

        players = set()
        scores = {}
        answer_time_sum = {}
        joined_at = {}

def start_game():
    with lock:
        if game["active"]:
            return False, "Game already running."

        if len(players) < MIN_PLAYERS:
            return False, f"❌ Need minimum {MIN_PLAYERS} players.\nCurrently: {len(players)}"

        # pick random questions for this game
        game["questions"] = random.sample(ALL_QUESTIONS, QUESTIONS_PER_GAME)
        game["active"] = True
        game["q_index"] = -1

        # init scores/time
        for p in players:
            scores.setdefault(p, 0)
            answer_time_sum.setdefault(p, 0.0)

    # announce start
    broadcast(f"🚀 LIVE QUIZ STARTED!\nPlayers competing: {len(players)}\nRules: 15 sec per question.\nReply A/B/C only.")
    send_next_question()
    return True, "✅ Game started."

def send_next_question():
    with lock:
        if not game["active"]:
            return

        game["q_index"] += 1
        idx = game["q_index"]

        if idx >= len(game["questions"]):
            # finished all questions
            schedule_finish_after_delay()
            return

        qobj = game["questions"][idx]
        game["question_open"] = True
        game["q_start_time"] = time.time()
        game["answered"] = set()
        game["fastest_correct"] = None

        # cancel previous timer if any
        if game["timer"]:
            try:
                game["timer"].cancel()
            except:
                pass

        # schedule question close after 15 seconds
        game["timer"] = threading.Timer(QUESTION_TIME_LIMIT, close_question)
        game["timer"].daemon = True
        game["timer"].start()

    # build and send question
    msg = (
        f"🧠 Question {idx+1}/{QUESTIONS_PER_GAME}\n"
        f"{qobj['question']}\n\n"
        f"A) {qobj['options']['A']}\n"
        f"B) {qobj['options']['B']}\n"
        f"C) {qobj['options']['C']}\n\n"
        f"⏱ Time: {QUESTION_TIME_LIMIT}s | Reply: A/B/C"
    )
    broadcast(msg)

def close_question():
    # called automatically after 15 seconds
    with lock:
        if not game["active"]:
            return
        if not game["question_open"]:
            return

        idx = game["q_index"]
        qobj = game["questions"][idx]
        correct = qobj["answer"].upper()
        game["question_open"] = False

        fastest = game["fastest_correct"]  # (phone, seconds) or None

    # announce correct answer
    fastest_line = ""
    if fastest:
        fastest_line = f"\n⚡ Fastest correct: {mask_number(fastest[0])} in {fastest[1]:.2f}s"
    broadcast(f"⏱ Time up!\n✅ Correct answer: {correct}{fastest_line}")

    # show leaderboard after each question
    broadcast(format_leaderboard("🏆 Leaderboard (after this question)"))

    # move to next question after 3 seconds
    t = threading.Timer(3, send_next_question)
    t.daemon = True
    t.start()

def schedule_finish_after_delay():
    # wait 15 sec like you asked, then declare results automatically
    t = threading.Timer(15, finish_game)
    t.daemon = True
    t.start()
    broadcast("✅ All questions done! Calculating results... (15 sec)")

def finish_game():
    with lock:
        if not game["active"]:
            return

        # finalize ranking: score desc, time asc
        ranking = []
        for p in players:
            sc = scores.get(p, 0)
            t = answer_time_sum.get(p, 10**9)
            ranking.append((p, sc, t))
        ranking.sort(key=lambda x: (-x[1], x[2]))

        # build result message
        lines = ["🏁 QUIZ FINISHED!", f"Players: {len(players)}", ""]
        lines.append("🥇 Winner:")
        if ranking:
            w = ranking[0]
            win_time = "-" if w[2] >= 10**9 else f"{w[2]:.1f}s"
            lines.append(f"{mask_number(w[0])} — {w[1]} pts — {win_time}")
        else:
            lines.append("No participants.")

        lines.append("")
        lines.append("🏆 Top 5:")
        for i, (p, sc, t) in enumerate(ranking[:5], start=1):
            time_str = "-" if t >= 10**9 else f"{t:.1f}s"
            lines.append(f"{i}) {mask_number(p)} — {sc} pts — {time_str}")

        result_msg = "\n".join(lines)

        # snapshot recipients
        recipients = list(players)

        # end game (keep lobby reset)
        # cancel timer
        if game["timer"]:
            try:
                game["timer"].cancel()
            except:
                pass
        game["active"] = False
        game["questions"] = []
        game["q_index"] = -1
        game["question_open"] = False
        game["q_start_time"] = None
        game["answered"] = set()
        game["fastest_correct"] = None
        game["timer"] = None

        # clear lobby too
        players.clear()
        scores.clear()
        answer_time_sum.clear()
        joined_at.clear()

    # broadcast results
    broadcast(result_msg, to_list=recipients)

# =========================
# ✅ ANSWER PROCESSING
# =========================
def handle_answer(sender: str, ans: str):
    with lock:
        if not game["active"] or not game["question_open"]:
            return "ℹ No active question right now. Type JOIN for next quiz."

        if sender not in players:
            return "Send JOIN to participate."

        if sender in game["answered"]:
            return "⚠️ You already answered this question."

        if ans not in ["A", "B", "C"]:
            return "Reply only A/B/C."

        game["answered"].add(sender)

        idx = game["q_index"]
        qobj = game["questions"][idx]
        correct = qobj["answer"].upper()

        # time taken from question start
        elapsed = max(0.0, time.time() - (game["q_start_time"] or time.time()))

        if ans == correct:
            scores[sender] = scores.get(sender, 0) + POINTS_PER_CORRECT
            answer_time_sum[sender] = answer_time_sum.get(sender, 0.0) + elapsed

            # fastest correct for this question
            fc = game["fastest_correct"]
            if (fc is None) or (elapsed < fc[1]):
                game["fastest_correct"] = (sender, elapsed)

            sc = scores[sender]
            return f"✅ Correct! (+{POINTS_PER_CORRECT})\nYour score: {sc}\nTime: {elapsed:.2f}s"
        else:
            return f"❌ Wrong! (Counted as wrong)\nTime: {elapsed:.2f}s"

# =========================
# ✅ WEBHOOK (GET + POST)
# =========================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # 1) Verification
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge
        return "Verification failed", 403

    # 2) Messages
    data = request.get_json()
    print("INCOMING:", data)

    try:
        value = data["entry"][0]["changes"][0]["value"]

        # Ignore delivery/read statuses
        if "messages" not in value:
            return jsonify({"status": "ignored"})

        msg = value["messages"][0]
        sender = msg["from"]

        if msg.get("type") != "text":
            send_message(sender, "⚠️ Only text supported.\nSend JOIN / HELP")
            return jsonify({"status": "non-text"})

        text = msg["text"]["body"].strip().upper()
    except Exception as e:
        print("ERROR parsing:", str(e))
        return jsonify({"status": "bad payload"})

    # =========================
    # ✅ COMMANDS
    # =========================

    # HELP
    if text in ["HELP", "MENU"]:
        send_message(
            sender,
            "📌 QUIZBOT INDIA\n"
            "JOIN = join today's quiz\n"
            "During quiz: reply A/B/C\n\n"
            "Admin: START / STATUS / RESET"
        )
        return jsonify({"status": "help"})

    # JOIN (lobby)
    if text == "JOIN":
        with lock:
            if game["active"]:
                send_message(sender, "❌ Quiz already started. Wait for next round.")
                return jsonify({"status": "late_join"})

            if sender not in players:
                players.add(sender)
                scores.setdefault(sender, 0)
                answer_time_sum.setdefault(sender, 0.0)
                joined_at[sender] = time.time()

                total = len(players)
            else:
                total = len(players)

        # tell the player
        need = max(0, MIN_PLAYERS - total)
        if need > 0:
            send_message(sender, f"✅ Joined!\nPlayers joined: {total}\nNeed {need} more to start.")
        else:
            send_message(sender, f"✅ Joined!\nPlayers joined: {total}\n🎉 Minimum reached! Waiting for START.")

        # optional: broadcast to all (competition vibe)
        broadcast(f"👤 New player joined! Total players now: {total}")

        # also notify admin
        send_message(ADMIN_NUMBER, f"👤 Joined: {mask_number(sender)} | Total: {total}")
        return jsonify({"status": "joined"})

    # ADMIN commands
    if sender == ADMIN_NUMBER:

        if text == "GAME":
            reset_lobby_and_game()
            broadcast(
            f"🎮 QUIZBOT INDIA\n\n"
            f"Game will start soon!\n"
            f"Send JOIN to participate.\n\n"
            f"Minimum players required: {MIN_PLAYERS}"
        )
            send_message(ADMIN_NUMBER, "✅ Lobby opened.")
            return jsonify({"status": "game_announced"})

    if text == "START":
        ok, msg2 = start_game()
        send_message(ADMIN_NUMBER, msg2)
        return jsonify({"status": "start_attempted"})

    # ANSWERS
    if text in ["A", "B", "C"]:
        reply = handle_answer(sender, text)
        send_message(sender, reply)
        return jsonify({"status": "answered"})

    # default
    send_message(sender, "Send JOIN to participate. Type HELP for commands.")
    return jsonify({"status": "ok"})

# =========================
# ✅ RUN SERVER
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)