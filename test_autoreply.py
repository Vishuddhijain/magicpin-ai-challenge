import urllib.request, json

def post(path, body):
    req = urllib.request.Request("http://localhost:8080"+path, data=json.dumps(body).encode(),
                                   headers={"Content-Type":"application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req).read())

msg = "Thank you for contacting us! Our team will respond shortly."
for i in range(1, 4):
    r = post("/v1/reply", {"conversation_id":"conv_fixed_test","merchant_id":"m_001_drmeera_dentist_delhi",
                             "customer_id":None,"from_role":"merchant","message":msg,
                             "received_at":"2026-04-26T10:00:00Z","turn_number":i+1})
    print(f"Turn {i}:", r["action"], "-", r.get("body","")[:60])