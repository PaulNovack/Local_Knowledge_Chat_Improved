const $ = id => document.getElementById(id);

const state = {
  page: 1,
  pageSize: Number($('page-size').value),
  total: 0,
  totalPages: 0,
  loading: false,
};

function setStatus(text) {
  $('status').textContent = text;
}

function setControls() {
  const disabled = state.loading;
  $('prev').disabled = disabled || state.page <= 1;
  $('prev-bottom').disabled = disabled || state.page <= 1;
  $('next').disabled = disabled || (state.totalPages > 0 && state.page >= state.totalPages);
  $('next-bottom').disabled = disabled || (state.totalPages > 0 && state.page >= state.totalPages);
  $('refresh').disabled = disabled;
  $('page-size').disabled = disabled;
  $('page-info').textContent = state.totalPages ? `Page ${state.page} of ${state.totalPages}` : 'No responses';
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
  $('page-size').value = String(state.pageSize);

  if (!data.items || !data.items.length) {
    renderEmpty(state.total ? 'No responses on this page yet.' : 'No chat history has been saved yet.');
  } else {
    $('responses').replaceChildren(...data.items.map(renderItem));
  }
  setControls();
}

async function loadResponses(page = state.page) {
  state.loading = true;
  setControls();
  setStatus('Loading responses…');
  try {
    const url = new URL('/api/responses', location.href);
    url.searchParams.set('page', String(page));
    url.searchParams.set('page_size', String(state.pageSize));
    const response = await fetch(url);
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || data.error || 'Unable to load responses');
    }
    render(data);
    setStatus(data.total ? `Showing ${data.items.length} of ${data.total} saved responses.` : 'No responses saved yet.');
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
  loadResponses(1);
});

$('refresh').onclick = () => loadResponses(state.page);
$('prev').onclick = () => { if (state.page > 1) loadResponses(state.page - 1); };
$('next').onclick = () => { if (!state.totalPages || state.page < state.totalPages) loadResponses(state.page + 1); };
$('prev-bottom').onclick = () => { if (state.page > 1) loadResponses(state.page - 1); };
$('next-bottom').onclick = () => { if (!state.totalPages || state.page < state.totalPages) loadResponses(state.page + 1); };
$('back-home').onclick = () => { location.href = '/'; };

loadResponses(1);