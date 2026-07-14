const API_BASE = import.meta.env.VITE_API_BASE_URL || '/api';
const MOCK_STATE_URL = '/mock/dashboard-state.json';

async function fetchJson(url, options = {}){
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options
  });
  if(!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  return res.json();
}

export async function getDashboardState(){
  try {
    return await fetchJson(`${API_BASE}/dashboard/state`);
  } catch (_err) {
    return fetchJson(MOCK_STATE_URL);
  }
}

export async function updateParameters(payload){
  try {
    return await fetchJson(`${API_BASE}/config/parameters`, {
      method: 'PATCH',
      body: JSON.stringify(payload)
    });
  } catch (_err) {
    return {
      saved: false,
      mode: 'mock',
      diff: payload.diff || {},
      message: 'API unavailable; kept as local preview only.'
    };
  }
}
