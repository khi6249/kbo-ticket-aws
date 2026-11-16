import os
import json
import uuid
import boto3
import redis
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Redis 연결
r = redis.Redis(host=os.getenv("REDIS_HOST", "redis"), port=6379, decode_responses=True)

# S3 클라이언트 설정 (EC2 IAM 역할 자동 인식)
S3_BUCKET = os.getenv("S3_BUCKET_NAME", "kbo-ticket-2224024")
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")
s3 = boto3.client("s3", region_name=AWS_REGION)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# 0. 헬스 체크 (슬라이드 2페이지 로컬 점검 필수 항목)
@app.route("/api/health", methods=["GET"])
def health_check():
    try:
        r.ping()
        return jsonify({"status": "ok", "redis": "connected", "s3_bucket": S3_BUCKET}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# 1. 경기/좌석 구역 등록 및 조회 (S3 시야 사진 업로드 연동)
@app.route("/api/sections", methods=["GET"])
def get_sections():
    section_keys = sorted(r.keys("section:*"))
    sections = []
    for key in section_keys:
        data = json.loads(r.get(key))
        seats_status = {}
        for seat in data["seats"]:
            lock_key = f"ticket:{data['id']}:{seat}"
            booked_info = r.get(lock_key)
            seats_status[seat] = json.loads(booked_info) if booked_info else None
        data["seats_status"] = seats_status
        sections.append(data)
    return jsonify(sections), 200


@app.route("/api/sections", methods=["POST"])
def create_section():
    title = request.form.get("title", "").strip()
    seats_raw = request.form.get("seats", "").strip()
    file = request.files.get("image")

    # 예외/경계 검증
    if not title or not seats_raw:
        return jsonify({"error": "경기/구역명과 예매 가능 좌석 번호를 모두 입력하세요."}), 400
    if not file or file.filename == "":
        return jsonify({"error": "좌석 시야 이미지 파일을 첨부해야 합니다."}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "허용되지 않는 파일 형식입니다. (png, jpg, jpeg, webp만 가능)"}), 400

    seats = [s.strip() for s in seats_raw.split(",") if s.strip()]
    if not seats:
        return jsonify({"error": "유효한 좌석 번호를 1개 이상 입력하세요."}), 400

    # S3 업로드
    ext = secure_filename(file.filename).rsplit(".", 1)[1].lower()
    s3_key = f"views/{uuid.uuid4().hex}.{ext}"
    try:
        s3.upload_fileobj(
            file,
            S3_BUCKET,
            s3_key,
            ExtraArgs={"ContentType": file.content_type},
        )
        image_url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
    except Exception as e:
        return jsonify({"error": f"S3 업로드 실패 (EC2 IAM 역할 연결 확인 필요): {str(e)}"}), 500

    section_id = uuid.uuid4().hex[:8]
    section_data = {
        "id": section_id,
        "title": title,
        "seats": seats,
        "image_url": image_url,
        "s3_key": s3_key,
    }
    r.set(f"section:{section_id}", json.dumps(section_data))
    return jsonify({"message": "경기 및 좌석 구역 등록 완료!", "section": section_data}), 201


# 2. 실시간 선착순 좌석 예매 (Redis SET NX 동시성 제어)
@app.route("/api/reserve", methods=["POST"])
def reserve_seat():
    data = request.get_json() or {}
    section_id = data.get("section_id", "").strip()
    seat = data.get("seat", "").strip()
    fan_id = data.get("fan_id", "").strip()
    fan_name = data.get("fan_name", "").strip()

    # 예외/경계 검증
    if not all([section_id, seat, fan_id, fan_name]):
        return jsonify({"error": "팬 ID(연락처), 이름, 구역, 좌석을 모두 입력하세요."}), 400

    section_raw = r.get(f"section:{section_id}")
    if not section_raw:
        return jsonify({"error": "존재하지 않는 경기/구역입니다."}), 404

    section_data = json.loads(section_raw)
    if seat not in section_data["seats"]:
        return jsonify({"error": "해당 구역에 존재하지 않는 좌석 번호입니다."}), 400

    lock_key = f"ticket:{section_id}:{seat}"
    ticket_payload = json.dumps({
        "section_id": section_id,
        "section_title": section_data["title"],
        "seat": seat,
        "fan_id": fan_id,
        "fan_name": fan_name,
    })

    # 원자적 선착순 처리 (nx=True: 키가 없을 때만 성공)
    is_success = r.set(lock_key, ticket_payload, nx=True)
    if not is_success:
        return jsonify({"error": "이미 선착순 매진된 좌석입니다."}), 409

    return jsonify({"message": f"[{section_data['title']} / {seat}석] 선착순 예매가 확정되었습니다!"}), 201


# 3. 내 티켓 조회 및 예매 취소
@app.route("/api/my-tickets", methods=["GET"])
def get_my_tickets():
    fan_id = request.args.get("fan_id", "").strip()
    if not fan_id:
        return jsonify({"error": "조회할 팬 ID(연락처)를 입력하세요."}), 400

    results = []
    for key in r.keys("ticket:*"):
        info = json.loads(r.get(key))
        if info["fan_id"] == fan_id:
            results.append(info)

    if not results:
        return jsonify({"error": "해당 ID로 예매된 티켓 내역이 없습니다."}), 404

    return jsonify(results), 200


@app.route("/api/cancel", methods=["POST"])
def cancel_ticket():
    data = request.get_json() or {}
    section_id = data.get("section_id", "").strip()
    seat = data.get("seat", "").strip()
    fan_id = data.get("fan_id", "").strip()

    if not all([section_id, seat, fan_id]):
        return jsonify({"error": "취소할 티켓 정보와 팬 ID를 입력하세요."}), 400

    lock_key = f"ticket:{section_id}:{seat}"
    booked_raw = r.get(lock_key)
    if not booked_raw:
        return jsonify({"error": "이미 취소되었거나 존재하지 않는 예매 내역입니다."}), 404

    booked_info = json.loads(booked_raw)
    if booked_info["fan_id"] != fan_id:
        return jsonify({"error": "본인의 팬 ID와 일치하지 않아 취소할 수 없습니다."}), 403

    r.delete(lock_key)
    return jsonify({"message": "티켓 예매가 정상적으로 취소되었습니다."}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)