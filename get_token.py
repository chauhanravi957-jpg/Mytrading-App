import requests

url = "https://api.upstox.com/v2/login/authorization/token"

payload = {
    "code": "ql8jEv",
    "client_id": "431300c1-0f1f-40a7-8968-6dfb147b2ba9",
    "client_secret": "q5dtkq1fr7",
    "redirect_uri": "http://37.187.140.45:8080/api/auth/callback"
    "grant_type": "authorization_code"
}

headers = {
    "accept": "application/json",
    "Api-Version": "2.0",
    "content-type": "application/x-www-form-urlencoded"
}

response = requests.post(url, data=payload, headers=headers)

print(response.json())