# Mask the card number in the JSON response, keeping the last four digits.
import json

body = json.loads(response.content)
card = str(body.get('card', ''))
body['card'] = '**** **** **** ' + card[-4:]
response.content = json.dumps(body)
