const $ = id => document.getElementById(id);
const initialParams = new URLSearchParams(location.search);
const initialPageSize = Number(initialParams.get('page_size') || $('page-size').value);
const initialPage = Number(initialParams.get('page') || '1');

const state = {
  page: Number.isFinite(initialPage) && initialPage > 0 ? initialPage : 1,
  pageSize: Number.isFinite(initialPageSize) && initialPageSize > 0 ? Math.min(initialPageSize, 50) : Number($('page-size').value),
  keyword: initialParams.get('q') || '',
  total: 0,
  totalPages: 0,
  loading: false,
};

function setStatus(text) {
  $('status').textContent = text;
}

function searchLabel() {
  return state.keyword.trim() ? ` for "${state.keyword.trim()}"` : '';
}

function syncUrl() {
  const url = new URL(location.href);
  url.searchParams.set('page', String(state.page));
  url.searchParams.set('page_size', String(state.pageSize));
  if (state.keyword.trim()) {
    url.searchParams.set('q', state.keyword.trim());
  } else {
    url.searchParams.delete('q');
  }
  history.replaceState({}, '', url);
}

function setControls() {
  const disabled = state.loading;
  $('prev').disabled = disabled || state.page <= 1;
  $('prev-bottom').disabled = disabled || state.page <= 1;
  $('next').disabled = disabled || (state.totalPages > 0 && state.page >= state.totalPages);
  $('next-bottom').disabled = disabled || (state.totalPages > 0 && state.page >= state.totalPages);
  $('refresh').disabled = disabled;
  $('page-size').disabled = disabled;
  $('search').disabled = disabled;
  $('run-search').disabled = disabled;
  $('clear-search').disabled = disabled || !state.keyword;
  $('page-info').textContent = state.totalPages
    ? `Page ${state.page} of ${state.totalPages}${searchLabel()}`
    : `No responses${searchLabel()}`;
}

function renderEmpty(message) {
  const empty = document.createElement('div');
  empty.className = 'empty-state';
  empty.textContent = message;
  $('responses').replaceChildren(empty);
}

function renderItem(item) {
  const card = document.createElement('article');
  card.className = 'response-card';

  const meta = document.createElement('div');
  meta.className = 'response-meta';

  const topic = document.createElement('span');
  topic.className = 'pill';
  topic.textContent = item.topic;

  const accepted = document.createElement('span');
  accepted.className = 'pill';
  accepted.textContent = item.accepted ? 'Accepted' : 'Pending';

  const created = document.createElement('span');
  created.textContent = `${item.created_at}`;

  meta.append(topic, accepted, created);

  const question = document.createElement('h2');
  question.textContent = item.question;

  const answer = document.createElement('p');
  answer.className = 'answer';
  answer.textContent = item.answer;

  const footer = document.createElement('div');
  footer.className = 'response-footer';
  const id = document.createElement('span');
  id.textContent = `Response #${item.ID}`;
  const date = document.createElement('span');
  date.textContent = `${item.created_date}`;
  footer.append(id, date);

  card.append(meta, question, answer, footer);
  return card;
}

function render(data) {
  state.total = data.total || 0;
  state.totalPages = data.total_pages || 0;
  state.page = data.page || 1;
  state.pageSize = data.page_size || state.pageSize;
  state.keyword = data.keyword || state.keyword || '';
  $('page-size').value = String(state.pageSize);
  $('search').value = state.keyword;

  if (!data.items || !data.items.length) {
    renderEmpty(state.total ? 'No responses on this page yet.' : 'No chat history has been saved yet.');
  } else {
    $('responses').replaceChildren(...data.items.map(renderItem));
  }
  setControls();
}

async function loadResponses(page = state.page) {
  state.page = page;
  syncUrl();
  state.loading = true;
  setControls();
  setStatus('Loading responses…');
  try {
    const url = new URL('/api/responses', location.href);
    url.searchParams.set('page', String(page));
    url.searchParams.set('page_size', String(state.pageSize));
    if (state.keyword.trim()) {
      url.searchParams.set('q', state.keyword.trim());
    }
    const response = await fetch(url);
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || data.error || 'Unable to load responses');
    }
    render(data);
    setStatus(data.total
      ? `Showing ${data.items.length} of ${data.total} saved responses${searchLabel()}.`
      : `No responses saved yet${searchLabel()}.`);
  } catch (error) {
    renderEmpty(error.message);
    setStatus(error.message);
  } finally {
    state.loading = false;
    setControls();
  }
}

$('page-size').addEventListener('change', () => {
  state.pageSize = Number($('page-size').value);
  state.page = 1;
  syncUrl();
  loadResponses(1);
});

$('search').addEventListener('input', () => {
  state.keyword = $('search').value;
});

$('search').addEventListener('keydown', event => {
  if (event.key === 'Enter') {
    event.preventDefault();
    $('search-form').requestSubmit();
  }
});

$('search-form').addEventListener('submit', event => {
  event.preventDefault();
  state.keyword = $('search').value;
  state.page = 1;
  syncUrl();
  loadResponses(1);
});

$('clear-search').onclick = () => {
  $('search').value = '';
  state.keyword = '';
  state.page = 1;
  syncUrl();
  loadResponses(1);
};

$('refresh').onclick = () => loadResponses(state.page);
$('prev').onclick = () => { if (state.page > 1) loadResponses(state.page - 1); };
$('next').onclick = () => { if (!state.totalPages || state.page < state.totalPages) loadResponses(state.page + 1); };
$('prev-bottom').onclick = () => { if (state.page > 1) loadResponses(state.page - 1); };
$('next-bottom').onclick = () => { if (!state.totalPages || state.page < state.totalPages) loadResponses(state.page + 1); };
$('back-home').onclick = () => { location.href = '/'; };

loadResponses(1);