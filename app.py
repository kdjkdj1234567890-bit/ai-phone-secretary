# -*- coding: utf-8 -*-
"""부재중 전화 잡는 AI 비서 - 데모 앱.

실행: ./run.sh  (또는 PORT=5007 python3 app.py)
"""

import json
import os
import traceback

from flask import Flask, jsonify, redirect, render_template, request, url_for

import db
from brain import (ShopBrain, booking_from_text, flow_handle, flow_prompt,
                   parse_phone)
from pricelist import INDUSTRIES

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "demo-only-key")


@app.template_filter("nl2br")
def nl2br(value):
    from markupsafe import Markup, escape
    # escape() 결과(Markup)에서 바로 replace하면 <br>까지 이스케이프되므로
    # 문자열로 바꾼 뒤 치환하고 Markup으로 감싼다
    return Markup(str(escape(value or "")).replace("\n", "<br>"))


# ---------------------------------------------------------------------------
# 두뇌 조립 (가게 설정 + 가격표를 DB에서 읽어 옴)
# ---------------------------------------------------------------------------

def build_brain(shop=None):
    shop = shop or db.get_shop()
    industry = shop["industry"] if shop["industry"] in INDUSTRIES else "cleaning"
    base = INDUSTRIES[industry]
    services = []
    for item in db.get_prices(industry):
        aliases = [a for a in (item["aliases"] or "").split(",") if a]
        services.append({
            "name": item["service"],
            "price": item["price"],
            "value": item["price_value"],
            "aliases": aliases or [item["service"]],
        })
    if not services:  # 가격표가 비어 있으면 기본값
        services = base["services"]
    data = dict(base)
    data["services"] = services
    return ShopBrain(industry, data, shop["name"]), industry


def money_text(value):
    """100000 → '10만원'."""
    value = int(value or 0)
    if value <= 0:
        return "0원"
    if value % 10000 == 0:
        return f"{value // 10000:,}만원"
    return f"{value // 10000:,}만 {value % 10000:,}원"


def summarize(text, length=40):
    t = (text or "").strip().replace("\n", " ")
    return t if len(t) <= length else t[:length] + "…"


# ---------------------------------------------------------------------------
# 손님 대화 처리
# ---------------------------------------------------------------------------

def handle_customer_message(inquiry_id, text):
    inq = db.get_inquiry(inquiry_id)
    if not inq:
        return None
    text = (text or "").strip()
    if not text:
        return "앗, 메시지가 비어 있어요. 다시 한 번 말씀해 주세요!"
    if len(text) > 500:
        text = text[:500]

    db.add_message(inquiry_id, "customer", text)
    if not inq["summary"]:
        db.update_inquiry(inquiry_id, summary=summarize(text))

    try:
        brain, industry = build_brain()
        step = inq["flow_step"] or "none"
        flow_data = json.loads(inq["flow_data"] or "{}")

        if step != "none":
            next_step, reply, done = flow_handle(step, text, brain, flow_data)
            db.update_inquiry(inquiry_id, flow_step=next_step,
                              flow_data=json.dumps(flow_data, ensure_ascii=False))
            db.add_message(inquiry_id, "ai", reply)
            if done:
                if step == "est_phone":
                    # 간이 견적 완료 → 예약이 아니라 전화 상담 연결
                    db.update_inquiry(
                        inquiry_id, status="callback",
                        summary=(f"간이 견적({flow_data.get('est_size_label', '')}·"
                                 f"{flow_data.get('est_dist_label', '')}, "
                                 f"예상 {flow_data.get('est_text', '')}) 후 전화 상담 요청"))
                    db.add_message(
                        inquiry_id, "system",
                        "📞 전화 상담 요청 — 사장님께 전달됐어요")
                else:
                    bid = db.create_booking(inquiry_id, flow_data)
                    designer = (f"({flow_data['designer']} 담당) "
                                if flow_data.get("designer") else "")
                    db.update_inquiry(
                        inquiry_id, status="booked",
                        summary=(f"{flow_data.get('service_name', '')} {designer}예약 "
                                 f"({flow_data.get('date_label', '')} "
                                 f"{flow_data.get('time_label', '')})"))
                    db.add_message(
                        inquiry_id, "system",
                        f"✅ 예약 접수됨 (예약번호 {bid}) — 사장님께 전달됐어요")
            return reply

        answer, action, needs_owner = brain.understand(text)
        if action == "booking":
            start_step = booking_from_text(text, brain, flow_data)
            db.update_inquiry(inquiry_id, flow_step=start_step,
                              flow_data=json.dumps(flow_data, ensure_ascii=False))
            reply = "예약 도와드릴게요! 😊\n" + flow_prompt(start_step, brain, flow_data)
            db.add_message(inquiry_id, "ai", reply)
            return reply
        if action == "estimate":
            db.update_inquiry(inquiry_id, flow_step="est_size",
                              flow_data=json.dumps(flow_data, ensure_ascii=False))
            reply = flow_prompt("est_size", brain, flow_data)
            db.add_message(inquiry_id, "ai", reply)
            return reply

        if needs_owner and inq["status"] == "open":
            db.update_inquiry(inquiry_id, status="callback")
        db.add_message(inquiry_id, "ai", answer)
        return answer
    except Exception:  # 어떤 경우에도 손님 화면에 오류를 노출하지 않음
        app.logger.exception("chat error")
        fallback = ("지금 잠깐 정신이 없네요 😅 사장님께 바로 전달해 드릴게요. "
                    "일 끝나시는 대로 연락드릴 거예요!")
        try:
            db.add_message(inquiry_id, "ai", fallback)
            db.update_inquiry(inquiry_id, status="callback")
        except Exception:
            pass
        return fallback


# ---------------------------------------------------------------------------
# 페이지
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", shop=db.get_shop(),
                           industries=INDUSTRIES)


@app.route("/about")
def about():
    """우리가 왜 만들었나요 — 레거시 철학."""
    return render_template("about.html")


@app.route("/simulation")
def simulation():
    return render_template("simulation.html")


@app.route("/chat/<int:inquiry_id>")
def chat(inquiry_id):
    inq = db.get_inquiry(inquiry_id)
    if not inq:
        return render_template("404.html"), 404
    messages = db.get_messages(inquiry_id)
    shop = db.get_shop()
    industry = shop["industry"] if shop["industry"] in INDUSTRIES else "cleaning"
    chips = {
        "cleaning": [("💰 가격표 보여줘", "가격표 보여줘"),
                     ("🏠 방문 견적 가능해요?", "방문 견적 가능해요?"),
                     ("📅 예약할게요", "예약할게요"),
                     ("🕘 영업시간 알려줘", "영업시간 알려줘")],
        "salon": [("💰 가격표 보여줘", "가격표 보여줘"),
                  ("💇 디자이너 지정할 수 있어요?", "디자이너 지정할 수 있어요?"),
                  ("📅 예약할게요", "예약할게요"),
                  ("⏱ 시술 시간 알려줘", "시술 시간 얼마나 걸려요?")],
        "moving": [("🧮 간이 견적 받고 싶어요", "간이 견적 받고 싶어요"),
                   ("💰 가격표 보여줘", "가격표 보여줘"),
                   ("📅 예약할게요", "예약할게요"),
                   ("🕗 영업시간 알려줘", "영업시간 알려줘")],
    }[industry]
    return render_template("chat.html", inquiry=inq, messages=messages,
                           shop=shop, chips=chips)


@app.route("/scenario")
def scenario():
    """핵심 시나리오 자동 시연: 청소 중인 사장님 → AI 응대 → 예약."""
    shop = db.get_shop()
    return render_template("scenario.html", shop=shop)


@app.route("/briefing")
def briefing():
    """사장님 오늘 브리핑: 아침 도파민 대시보드."""
    stats = db.today_stats()
    shop = db.get_shop()
    industry = shop["industry"] if shop["industry"] in INDUSTRIES else "cleaning"
    return render_template("briefing.html", stats=stats,
                           industry_label=INDUSTRIES[industry]["label"])


@app.route("/dashboard")
def dashboard():
    from datetime import date
    stats = db.today_stats()
    month = db.month_stats()
    inquiries = db.list_inquiries(limit=30)
    bookings = db.list_bookings(limit=30)
    shop = db.get_shop()
    industry = shop["industry"] if shop["industry"] in INDUSTRIES else "cleaning"
    industry_label = INDUSTRIES[industry]["label"]
    # 경쟁 FOMO: 날짜에 따라 조금씩 바뀌는 예시 수치
    fomo_count = 12 + (date.today().day % 5)
    # 월간 리포트: 동네 평균 대비 예약 전환율 (예시 벤치마크 35%)
    conv = (month["bookings"] / month["inquiries"]) if month["inquiries"] else 0
    vs_avg = round(conv / 0.35 * 100) if conv else 0
    return render_template("dashboard.html", stats=stats, month=month,
                           inquiries=inquiries, bookings=bookings,
                           money_text=money_text, industry_label=industry_label,
                           fomo_count=fomo_count, vs_avg=vs_avg)


@app.route("/history")
def history():
    inquiries = db.list_inquiries(limit=100)
    counts = {}
    for inq in inquiries:
        counts[inq["id"]] = len(db.get_messages(inq["id"]))
    return render_template("history.html", inquiries=inquiries, counts=counts)


@app.route("/history/<int:inquiry_id>")
def history_detail(inquiry_id):
    inq = db.get_inquiry(inquiry_id)
    if not inq:
        return render_template("404.html"), 404
    messages = db.get_messages(inquiry_id)
    booking = db.get_booking_by_inquiry(inquiry_id)
    return render_template("history_detail.html", inquiry=inq,
                           messages=messages, booking=booking,
                           money_text=money_text)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    shop = db.get_shop()
    if request.method == "POST":
        try:
            name = request.form.get("name", "")
            industry = request.form.get("industry", "cleaning")
            if industry not in INDUSTRIES:
                industry = "cleaning"
            greeting = request.form.get("greeting", "")
            sms_text = request.form.get("sms_text", "")
            db.save_shop(name, industry, greeting, sms_text)

            items = []
            services = request.form.getlist("service[]")
            prices = request.form.getlist("price[]")
            values = request.form.getlist("value[]")
            for s, p, v in zip(services, prices, values):
                s, p = s.strip(), p.strip()
                if not s:
                    continue
                try:
                    vv = int((v or "0").replace(",", ""))
                except ValueError:
                    vv = 0
                items.append((s, p or "별도 상담", vv))
            if items:
                db.save_prices(industry, items)
            return redirect(url_for("settings", saved="1"))
        except Exception:
            app.logger.exception("settings save error")
            return render_template("settings.html", shop=shop,
                                   industries=INDUSTRIES,
                                   prices=db.get_prices(shop["industry"]),
                                   referral_code=db.get_referral_code(),
                                   error="저장하다가 문제가 생겼어요. 다시 시도해 주세요.")
    prices = db.get_prices(shop["industry"])
    return render_template("settings.html", shop=shop, industries=INDUSTRIES,
                           prices=prices,
                           referral_code=db.get_referral_code(),
                           saved=request.args.get("saved"))


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route("/api/simulation/start", methods=["POST"])
def api_simulation_start():
    try:
        shop = db.get_shop()
        inquiry_id = db.create_inquiry(customer_name="체험 손님",
                                      customer_phone="010-****-0000",
                                      industry=shop["industry"])
        sms_text = (shop["sms_text"] or "").strip() or \
            "안녕하세요! 지금은 통화 중이라 전화를 못 받았어요 🙏 " \
            "여기에 문자로 남겨주시면 바로 답해 드릴게요!"
        db.add_message(inquiry_id, "system",
                       "📵 부재중 전화 1통 — 사장님이 일 중이라 받지 못했어요")
        db.add_message(inquiry_id, "ai", sms_text)
        db.update_inquiry(inquiry_id, summary="부재중 전화 → 자동 문자 발송됨")
        return jsonify({"ok": True, "inquiry_id": inquiry_id,
                        "sms_text": sms_text})
    except Exception:
        app.logger.exception("simulation start error")
        return jsonify({"ok": False,
                        "error": "지금 잠깐 문제가 생겼어요. 다시 눌러주세요."}), 500


@app.route("/api/chat/<int:inquiry_id>/message", methods=["POST"])
def api_chat_message(inquiry_id):
    try:
        data = request.get_json(force=True, silent=True) or {}
        reply = handle_customer_message(inquiry_id, data.get("text", ""))
        if reply is None:
            return jsonify({"ok": False,
                            "error": "대화를 찾을 수 없어요."}), 404
        inq = db.get_inquiry(inquiry_id)
        return jsonify({"ok": True, "reply": reply,
                        "status": inq["status"] if inq else "open"})
    except Exception:
        app.logger.exception("chat api error")
        return jsonify({"ok": False,
                        "error": "지금 잠깐 문제가 생겼어요. 다시 보내주세요."}), 500


@app.route("/api/seed/clear", methods=["POST"])
def api_seed_clear():
    try:
        n = db.clear_seed()
        return jsonify({"ok": True, "cleared": n})
    except Exception:
        app.logger.exception("seed clear error")
        return jsonify({"ok": False,
                        "error": "지우다가 문제가 생겼어요."}), 500


@app.route("/api/referral", methods=["POST"])
def api_referral():
    """추천 링크 만들기 (시뮬레이션): 옆 가게 사장님께 소개하면 둘 다 1개월 무료."""
    import random
    import string
    try:
        code = db.get_referral_code()
        if not code:
            code = "".join(random.choice(string.ascii_uppercase + string.digits)
                           for _ in range(6))
            db.set_referral_code(code)
        shop = db.get_shop()
        return jsonify({"ok": True, "code": code,
                        "link": f"https://aibiseo.demo/r/{code}",
                        "shop_name": shop["name"]})
    except Exception:
        app.logger.exception("referral error")
        return jsonify({"ok": False,
                        "error": "링크를 만들다가 문제가 생겼어요. 다시 눌러주세요."}), 500


@app.route("/api/industry", methods=["POST"])
def api_industry():
    """첫 화면 '어떤 가게세요?' 선택 → 가게 업종 변경."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        industry = data.get("industry", "")
        if industry not in INDUSTRIES:
            return jsonify({"ok": False,
                            "error": "업종을 다시 골라주세요."}), 400
        shop = db.get_shop()
        db.save_shop(shop["name"], industry, shop["greeting"], shop["sms_text"])
        return jsonify({"ok": True, "industry": industry,
                        "label": INDUSTRIES[industry]["label"],
                        "scenario": INDUSTRIES[industry].get("scenario", "")})
    except Exception:
        app.logger.exception("industry switch error")
        return jsonify({"ok": False,
                        "error": "바꾸다가 문제가 생겼어요. 다시 눌러주세요."}), 500


# ---------------------------------------------------------------------------
# 오류 화면 (기술 용어 노출 금지)
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("500.html"), 500


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def find_port(start=5007, tries=10):
    import socket
    for p in range(start, start + tries):
        s = socket.socket()
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", p))
            s.close()
        except OSError:
            return p
    return start + tries


if __name__ == "__main__":
    db.init_db(seed=True)
    port = int(os.environ.get("PORT", 0) or find_port(5007))
    print(f"▶ http://localhost:{port} 에서 실행 중", flush=True)
    app.run(host="0.0.0.0", port=port)
