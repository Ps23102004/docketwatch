const request = async (path) => { const response = await fetch(path); const data = await response.json(); if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`); return data; };
export const getCases = () => request('/api/cases');
export const getDigest = (id) => request(`/api/cases/${encodeURIComponent(id)}/digest`);
