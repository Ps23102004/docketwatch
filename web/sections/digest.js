import { getDigest } from '../lib/api.js';

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
export async function renderDigest(root, docketId) {
  root.innerHTML = '<p class="state">Loading digest…</p>';
  try {
    const data = await getDigest(docketId);
    const fields = ['VERDICT', 'CONSENSUS', 'DISAGREEMENTS'].filter((key) => data[key] != null);
    root.innerHTML = `<a class="back" href="#cases">← All cases</a><div class="toolbar"><div><p class="eyebrow">AI case digest</p><h1>${esc(docketId)}</h1></div></div>${fields.length ? fields.map((key) => `<section class="digest-block"><span class="tag">${key}</span><p>${esc(data[key]).replace(/\n/g, '<br>')}</p></section>`).join('') : `<section class="digest-block"><div class="meta">Filing summary</div><p>${esc(data.markdown || 'No digest available.').replace(/\n/g, '<br>')}</p></section>`}`;
  } catch (error) { root.innerHTML = `<a class="back" href="#cases">← All cases</a><p class="error">${esc(error.message)}</p>`; }
}
