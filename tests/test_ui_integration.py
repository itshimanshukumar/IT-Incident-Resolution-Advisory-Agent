from fastapi.testclient import TestClient

from agent.main import app

client = TestClient(app)


def test_existing_agent_routes_remain_available():
    assert client.get('/health').status_code == 200
    response = client.post('/check-outage', json={'location': 'Chennai', 'service': 'Wi-Fi'})
    assert response.status_code == 200
    assert response.json()['status'] in {'active', 'resolved', 'unknown'}


def test_both_portals_are_served_from_renamed_ui_directory():
    employee = client.get('/employee-ui/')
    it_staff = client.get('/it_department/')
    assert employee.status_code == 200
    assert 'ResolveIT' in employee.text
    assert it_staff.status_code == 200
    assert 'IT' in it_staff.text
    assert client.get('/employee', follow_redirects=False).headers['location'] == '/employee-ui/'
    assert client.get('/it', follow_redirects=False).headers['location'] == '/it_department/'


def test_ui_api_authentication_and_portal_separation():
    health = client.get('/api/health')
    assert health.status_code == 200
    assert health.json()['database'] == 'connected'

    employee_login = client.post('/api/auth/login', json={
        'employee_id': 'E1001', 'password': 'Employee@123', 'portal': 'employee'
    })
    assert employee_login.status_code == 200
    employee_token = employee_login.json()['access_token']

    denied_it_login = client.post('/api/auth/login', json={
        'employee_id': 'E1001', 'password': 'Employee@123', 'portal': 'it'
    })
    assert denied_it_login.status_code == 401

    forbidden = client.get('/api/dashboard/metrics', headers={
        'Authorization': f'Bearer {employee_token}'
    })
    assert forbidden.status_code == 403

    unauthenticated = client.get('/api/dashboard/metrics', headers={
        'x-user-role': 'it'
    })
    assert unauthenticated.status_code == 401


def test_ui_ticket_listing_requires_verified_identity_or_guest_session():
    assert client.get('/api/tickets').status_code == 401

    login = client.post('/api/auth/login', json={
        'employee_id': 'E1001', 'password': 'Employee@123', 'portal': 'employee'
    })
    token = login.json()['access_token']
    response = client.get('/api/tickets', headers={'Authorization': f'Bearer {token}'})
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_employee_chat_uses_resolveit_advisory_backend():
    response = client.post('/api/chat/message', json={
        'message': 'My Wi-Fi is not working in Chennai',
        'employee_id': 'E1001',
        'location': 'Chennai',
        'service': 'Wi-Fi',
        'os_name': 'Windows',
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload['conversation_id']
    assert payload['outage_status'] == 'Active'
    assert 'Network controller fault' in payload['agent_message']
