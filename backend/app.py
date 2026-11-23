import os
import json
import uuid
import re
from datetime import date, datetime, timezone, timedelta
import boto3
import redis
from flask import Flask, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# 한국 표준시(KST, UTC+9)
KST = timezone(timedelta(hours=9))

# Redis 연결
r = redis.Redis(host=os.getenv("REDIS_HOST", "redis"), port=6379, decode_responses=True)

# S3 클라이언트 설정 (EC2 IAM 역할 자동 인식)
S3_BUCKET = os.getenv("S3_BUCKET_NAME", "kbo-ticket-2224024")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-2")
s3 = boto3.client("s3", region_name=AWS_REGION)

# 관리자 계정 설정 (환경변수 또는 기본값)
ADMIN_ID = os.getenv("ADMIN_ID", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "kbo2026!")

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def parse_kst_datetime(dt_str):
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except ValueError:
        return None


def is_section_open(section_data):
    open_dt = parse_kst_datetime(section_data.get("open_at", ""))
    if not open_dt:
        return True, None
    now_kst = datetime.now(KST)
    return now_kst >= open_dt, open_dt


def verify_admin_token():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header.replace("Bearer ", "", 1).strip()
    if not token:
        return False
    return r.exists(f"admin_session:{token}") == 1


# 서버 시작 시 등록된 구역이 없으면 기본 KBO 경기 좌석 프리셋 자동 등록
def init_default_sections():
    try:
        if len(r.keys("section:*")) > 0:
            return
        today_str = date.today().isoformat()
        defaults = [
            {
                "id": "jamsil-blue",
                "title": f"[{today_str}] 잠실야구장 (LG vs KT) - 1루 블루석 (207블록)",
                "game_date": today_str,
                "stadium": "잠실야구장",
                "open_at": "",
                "price": 22000,
                "seats": [
                    "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8",
                    "B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8",
                    "C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8"
                ],
                "image_url": "https://images.unsplash.com/photo-1508344928928-7165b67de128?auto=format&fit=crop&w=800&q=80",
                "s3_key": "",
            },
            {
                "id": "jamsil-navy",
                "title": f"[{today_str}] 잠실야구장 (LG vs KT) - 1루 네이비석 (306블록)",
                "game_date": today_str,
                "stadium": "잠실야구장",
                "open_at": "",
                "price": 15000,
                "seats": [
                    "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8",
                    "B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8"
                ],
                "image_url": "https://images.unsplash.com/photo-1566577739112-5180d4bf9390?auto=format&fit=crop&w=800&q=80",
                "s3_key": "",
            },
        ]
        for sec in defaults:
            r.set(f"section:{sec['id']}", json.dumps(sec))
    except Exception:
        pass


init_default_sections()


# 0. 헬스 체크
@app.route("/api/health", methods=["GET"])
def health_check():
    try:
        r.ping()
        return jsonify({
            "status": "ok",
            "redis": "connected",
            "s3_bucket": S3_BUCKET,
            "server_time_kst": datetime.now(KST).isoformat()
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# 관리자 로그인 / 세션 검증 / 로그아웃
@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if username != ADMIN_ID or password != ADMIN_PASSWORD:
        return jsonify({"error": "관리자 아이디 또는 비밀번호가 일치하지 않습니다."}), 401

    token = uuid.uuid4().hex
    r.setex(f"admin_session:{token}", 7200, username)  # 2시간 유효 세션
    return jsonify({"message": "관리자 로그인 성공", "token": token, "admin_id": username}), 200


@app.route("/api/admin/verify", methods=["GET"])
def admin_verify():
    if not verify_admin_token():
        return jsonify({"authenticated": False}), 401
    return jsonify({"authenticated": True}), 200


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.replace("Bearer ", "", 1).strip()
        if token:
            r.delete(f"admin_session:{token}")
    return jsonify({"message": "로그아웃되었습니다."}), 200


# 1. 경기/좌석 구역 조회 (공개), 등록·삭제 (관리자 인증 필수)
@app.route("/api/sections", methods=["GET"])
def get_sections():
    section_keys = sorted(r.keys("section:*"))
    sections = []
    for key in section_keys:
        data = json.loads(r.get(key))
        seats_status = {}
        for seat in data["seats"]:
            lock_key = f"ticket:{data['id']}:{seat}"
            booked_raw = r.get(lock_key)
            if booked_raw:
                booked_info = json.loads(booked_raw)
                seats_status[seat] = {
                    "fan_name": booked_info.get("fan_name"),
                    "booking_code": booked_info.get("booking_code"),
                }
            else:
                seats_status[seat] = None
        opened, _ = is_section_open(data)
        data["is_open"] = opened
        data["seats_status"] = seats_status
        sections.append(data)
    return jsonify(sections), 200


@app.route("/api/sections", methods=["POST"])
def create_section():
    if not verify_admin_token():
        return jsonify({"error": "관리자 로그인이 필요합니다."}), 401

    title = request.form.get("title", "").strip()
    game_date = request.form.get("game_date", "").strip()
    stadium = request.form.get("stadium", "").strip()
    open_at = request.form.get("open_at", "").strip()
    seats_raw = request.form.get("seats", "").strip()
    price_raw = request.form.get("price", "15000").strip()
    file = request.files.get("image")

    if not title or not seats_raw:
        return jsonify({"error": "경기/구역명과 예매 가능 좌석 번호를 모두 입력하세요."}), 400
    if len(title) > 70:
        return jsonify({"error": "경기/구역명은 70자 이하로 입력하세요."}), 400
    if open_at and not parse_kst_datetime(open_at):
        return jsonify({"error": "티켓 오픈 일시 형식이 올바르지 않습니다."}), 400

    try:
        price = int(price_raw)
        if price < 0 or price > 1000000:
            return jsonify({"error": "좌석 가격은 0원 이상 1,000,000원 이하로 입력하세요."}), 400
    except ValueError:
        return jsonify({"error": "좌석 가격은 숫자로 입력하세요."}), 400

    if not file or file.filename == "":
        return jsonify({"error": "좌석 시야 이미지 파일을 첨부해야 합니다."}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "허용되지 않는 파일 형식입니다. (png, jpg, jpeg, gif, webp만 가능)"}), 400

    seats = list(dict.fromkeys([s.strip() for s in seats_raw.split(",") if s.strip()]))
    if not seats:
        return jsonify({"error": "유효한 좌석 번호를 1개 이상 입력하세요."}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    s3_key = f"views/{uuid.uuid4().hex}.{ext}"
    try:
        s3.upload_fileobj(
            file,
            S3_BUCKET,
            s3_key,
            ExtraArgs={"ContentType": file.content_type or "image/jpeg"},
        )
        image_url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
    except Exception as e:
        return jsonify({"error": f"S3 업로드 실패: {str(e)}"}), 500

    section_id = uuid.uuid4().hex[:8]
    section_data = {
        "id": section_id,
        "title": title,
        "game_date": game_date,
        "stadium": stadium,
        "open_at": open_at,
        "price": price,
        "seats": seats,
        "image_url": image_url,
        "s3_key": s3_key,
    }
    r.set(f"section:{section_id}", json.dumps(section_data))
    return jsonify({"message": "경기 및 좌석 구역이 등록되었습니다!", "section": section_data}), 201


@app.route("/api/sections/<section_id>", methods=["DELETE"])
def delete_section(section_id):
    if not verify_admin_token():
        return jsonify({"error": "관리자 로그인이 필요합니다."}), 401

    section_raw = r.get(f"section:{section_id}")
    if not section_raw:
        return jsonify({"error": "존재하지 않는 경기/구역입니다."}), 404

    section_data = json.loads(section_raw)
    try:
        if section_data.get("s3_key"):
            s3.delete_object(Bucket=S3_BUCKET, Key=section_data["s3_key"])
    except Exception:
        pass

    r.delete(f"section:{section_id}")
    for seat in section_data.get("seats", []):
        r.delete(f"ticket:{section_id}:{seat}")

    return jsonify({"message": "경기 구역이 삭제되었습니다."}), 200


# 2-A. 단건 선착순 좌석 예매 (사용자 지정 4자리 예매 비밀번호)
@app.route("/api/reserve", methods=["POST"])
def reserve_seat():
    data = request.get_json() or {}
    section_id = data.get("section_id", "").strip()
    seat = data.get("seat", "").strip()
    fan_id = data.get("fan_id", "").strip()
    fan_name = data.get("fan_name", "").strip()
    pin_code = data.get("pin_code", "").strip()

    if not all([section_id, seat, fan_id, fan_name, pin_code]):
        return jsonify({"error": "연락처, 이름, 예매 비밀번호(4자리), 좌석을 모두 입력하세요."}), 400
    if len(fan_id) > 30 or len(fan_name) > 20:
        return jsonify({"error": "연락처는 30자, 이름은 20자 이하로 입력하세요."}), 400
    if not re.fullmatch(r"\d{4}", pin_code):
        return jsonify({"error": "예매 비밀번호는 숫자 4자리로 설정해야 합니다."}), 400

    section_raw = r.get(f"section:{section_id}")
    if not section_raw:
        return jsonify({"error": "존재하지 않는 경기/구역입니다."}), 404

    section_data = json.loads(section_raw)
    opened, open_dt = is_section_open(section_data)
    if not opened:
        return jsonify({"error": f"아직 티켓 오픈 전입니다. (오픈 일시: {open_dt.strftime('%Y-%m-%d %H:%M')})"}), 403

    if seat not in section_data["seats"]:
        return jsonify({"error": "해당 구역에 존재하지 않는 좌석 번호입니다."}), 400

    price = section_data.get("price", 0)
    booking_code = f"KBO-{uuid.uuid4().hex[:6].upper()}"
    pin_hash = generate_password_hash(pin_code)

    lock_key = f"ticket:{section_id}:{seat}"
    ticket_payload = json.dumps({
        "booking_code": booking_code,
        "pin_hash": pin_hash,
        "section_id": section_id,
        "section_title": section_data["title"],
        "seat": seat,
        "price": price,
        "fan_id": fan_id,
        "fan_name": fan_name,
    })

    if not r.set(lock_key, ticket_payload, nx=True):
        return jsonify({"error": "이미 선착순 매진된 좌석입니다."}), 409

    return jsonify({
        "message": f"[{section_data['title']} / {seat}석] 예매 확정! (결제금액: {price:,}원)",
        "booking_code": booking_code,
        "total_amount": price,
    }), 201


# 2-B. 다중 좌석 일괄 선착순 예매 (사용자 지정 4자리 예매 비밀번호)
@app.route("/api/reserve-batch", methods=["POST"])
def reserve_batch():
    data = request.get_json() or {}
    items = data.get("items", [])
    fan_id = data.get("fan_id", "").strip()
    fan_name = data.get("fan_name", "").strip()
    pin_code = data.get("pin_code", "").strip()

    if not fan_id or not fan_name or not pin_code:
        return jsonify({"error": "연락처, 예매자 이름, 설정할 예매 비밀번호(4자리)를 모두 입력하세요."}), 400
    if len(fan_id) > 30 or len(fan_name) > 20:
        return jsonify({"error": "연락처는 30자, 이름은 20자 이하로 입력하세요."}), 400
    if not re.fullmatch(r"\d{4}", pin_code):
        return jsonify({"error": "예매 비밀번호는 숫자 4자리로 설정해야 합니다. (예: 1234)"}), 400
    if not items or not isinstance(items, list):
        return jsonify({"error": "예매할 좌석을 1개 이상 선택하세요."}), 400

    booking_code = f"KBO-{uuid.uuid4().hex[:6].upper()}"
    pin_hash = generate_password_hash(pin_code)

    succeeded = []
    failed = []
    total_amount = 0

    for item in items:
        section_id = str(item.get("section_id", "")).strip()
        seat = str(item.get("seat", "")).strip()
        section_raw = r.get(f"section:{section_id}")
        if not section_raw:
            failed.append(f"{seat}석(구역 없음)")
            continue

        section_data = json.loads(section_raw)
        opened, open_dt = is_section_open(section_data)
        if not opened:
            failed.append(f"{seat}석({open_dt.strftime('%H:%M')} 오픈 예정)")
            continue

        if seat not in section_data["seats"]:
            failed.append(f"{seat}석(없는 좌석)")
            continue

        price = section_data.get("price", 0)
        lock_key = f"ticket:{section_id}:{seat}"
        ticket_payload = json.dumps({
            "booking_code": booking_code,
            "pin_hash": pin_hash,
            "section_id": section_id,
            "section_title": section_data["title"],
            "seat": seat,
            "price": price,
            "fan_id": fan_id,
            "fan_name": fan_name,
        })

        if r.set(lock_key, ticket_payload, nx=True):
            succeeded.append(f"{section_data['title']} {seat}석")
            total_amount += price
        else:
            failed.append(f"{seat}석(선착순 매진)")

    if not succeeded:
        return jsonify({"error": f"예매 불가: {', '.join(failed)}"}), 403

    msg = f"총 {len(succeeded)}석 예매 확정! (결제금액: {total_amount:,}원)"
    if failed:
        msg += f" / 제외: {', '.join(failed)}"

    return jsonify({
        "message": msg,
        "booking_code": booking_code,
        "succeeded": succeeded,
        "failed": failed,
        "total_amount": total_amount,
    }), 201


# 3. 내 티켓 조회 및 예매 취소 (연락처 + 사용자가 설정한 4자리 비밀번호 검증)
@app.route("/api/my-tickets", methods=["GET"])
def get_my_tickets():
    fan_id = request.args.get("fan_id", "").strip()
    pin_code = request.args.get("pin_code", "").strip()

    if not fan_id or not pin_code:
        return jsonify({"error": "연락처와 예매 시 설정한 4자리 비밀번호를 모두 입력하세요."}), 400

    fan_tickets = []
    for key in r.keys("ticket:*"):
        info = json.loads(r.get(key))
        if info["fan_id"] == fan_id:
            fan_tickets.append(info)

    if not fan_tickets:
        return jsonify({"error": "해당 연락처로 예매된 티켓 내역이 없습니다."}), 404

    verified_tickets = []
    for t in fan_tickets:
        if t.get("pin_hash") and check_password_hash(t["pin_hash"], pin_code):
            verified_tickets.append({
                "booking_code": t.get("booking_code", "-"),
                "section_id": t["section_id"],
                "section_title": t["section_title"],
                "seat": t["seat"],
                "price": t.get("price", 0),
                "fan_id": t["fan_id"],
                "fan_name": t["fan_name"],
            })

    if not verified_tickets:
        return jsonify({"error": "예매 비밀번호(4자리)가 일치하지 않습니다."}), 401

    return jsonify(verified_tickets), 200


@app.route("/api/cancel", methods=["POST"])
def cancel_ticket():
    data = request.get_json() or {}
    section_id = data.get("section_id", "").strip()
    seat = data.get("seat", "").strip()
    fan_id = data.get("fan_id", "").strip()
    pin_code = data.get("pin_code", "").strip()

    if not all([section_id, seat, fan_id, pin_code]):
        return jsonify({"error": "취소할 티켓 정보, 연락처, 예매 비밀번호를 모두 입력하세요."}), 400

    lock_key = f"ticket:{section_id}:{seat}"
    booked_raw = r.get(lock_key)
    if not booked_raw:
        return jsonify({"error": "이미 취소되었거나 존재하지 않는 예매 내역입니다."}), 404

    booked_info = json.loads(booked_raw)
    if booked_info["fan_id"] != fan_id:
        return jsonify({"error": "본인의 연락처와 일치하지 않아 취소할 수 없습니다."}), 403

    if not booked_info.get("pin_hash") or not check_password_hash(booked_info["pin_hash"], pin_code):
        return jsonify({"error": "예매 비밀번호(4자리)가 일치하지 않아 취소할 수 없습니다."}), 401

    r.delete(lock_key)
    return jsonify({"message": "티켓 예매가 안전하게 취소되었습니다."}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)