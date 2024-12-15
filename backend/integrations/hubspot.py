from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
import secrets
import json
import base64
import asyncio
import httpx

import requests
from integrations.hubspot_integration_item import HubspotIntegrationItem
from redis_client import add_key_value_redis, get_value_redis, delete_key_redis

# Get these HubSpot credentials from this site -> https://app.hubspot.com/developer/48554327/applications
CLIENT_ID = 'XXX'
CLIENT_SECRET = 'XXX'

SCOPE = 'oauth crm.objects.contacts.read'

AUTHORIZATION_URI = f'https://app.hubspot.com/oauth/authorize'
REDIRECT_URI='http://localhost:8000/integrations/hubspot/oauth2callback'

async def authorize_hubspot(user_id, org_id):
    state_data = {
        'state': secrets.token_urlsafe(32),
        'user_id': user_id,
        'org_id': org_id,
    }
    encoded_state = base64.urlsafe_b64encode(json.dumps(state_data).encode('utf-8')).decode('utf-8')
    auth_url = f'{AUTHORIZATION_URI}?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&scope={SCOPE}&state={encoded_state}'
    await asyncio.gather(
        add_key_value_redis(f'hubspot_state:{org_id}:{user_id}', json.dumps(state_data), expire=600),
    )
    return auth_url

async def oauth2callback_hubspot(request: Request):
    if request.query_params.get('error'):
        raise HTTPException(status_code=400, detail=request.query_params.get('error_description'))
    
    code = request.query_params.get('code')
    encoded_state = request.query_params.get('state')
    state_data = json.loads(base64.urlsafe_b64decode(encoded_state).decode('utf-8'))
    original_state = state_data.get('state')
    user_id = state_data.get('user_id')
    org_id = state_data.get('org_id')
    saved_state = await asyncio.gather(
        get_value_redis(f'hubspot_state:{org_id}:{user_id}')
    )
    
    if not saved_state or original_state != json.loads(saved_state[0]).get('state'):
        raise HTTPException(status_code=400, detail='State does not match.')
    
    async with httpx.AsyncClient() as client:
        response, _ = await asyncio.gather(
            client.post(
                'https://api.hubapi.com/oauth/v1/token',
                data={
                    'grant_type': 'authorization_code',
                    'code': code,
                    'redirect_uri': REDIRECT_URI,
                    'client_id': CLIENT_ID,
                    'client_secret': CLIENT_SECRET
                },
            ),
            delete_key_redis(f'hubspot_state:{org_id}:{user_id}'),
        )
    
    await add_key_value_redis(f'hubspot_credentials:{org_id}:{user_id}', json.dumps(response.json()), expire=600)

    close_window_script = """
    <html>
        <script>
            window.close();
        </script>
    </html>
    """
    return HTMLResponse(content=close_window_script)

async def get_hubspot_credentials(user_id, org_id):
    credentials = await get_value_redis(f'hubspot_credentials:{org_id}:{user_id}')
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    credentials = json.loads(credentials)
    await delete_key_redis(f'hubspot_credentials:{org_id}:{user_id}')
    
    return credentials

def create_integration_item_metadata_object(response_json) -> HubspotIntegrationItem:
    contact_integration_item_metadata = HubspotIntegrationItem(
        id=response_json.get('id', None),
        createdAt=response_json.get('createdAt'),
        updatedAt=response_json.get('updatedAt'),
        firstName=response_json.get('properties').get('firstname'),
        lastName=response_json.get('properties').get('lastname'),
        email=response_json.get('properties').get('email'),
        archived=response_json.get('archived'),
    )
    return contact_integration_item_metadata

def fetch_items(
    access_token: str, url: str, aggregated_response: list
):
    """Fetching the list of objects"""
    headers = {'Authorization': f'Bearer {access_token}'}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        result = response.json().get('results')
        for item in result:
            aggregated_response.append(item)
    else:
        return

async def get_items_hubspot(credentials):
    credentials = json.loads(credentials)
    url = 'https://api.hubapi.com/crm/v3/objects/contacts'
    list_of_responses = []
    list_of_contact_integration_item_metadata = []
    fetch_items(credentials.get('access_token'), url, list_of_responses)
    for response in list_of_responses:
        list_of_contact_integration_item_metadata.append(
            create_integration_item_metadata_object(response)
        )
    print(f'list_of_contact_integration_item_metadata: {list_of_contact_integration_item_metadata}')
    return list_of_contact_integration_item_metadata