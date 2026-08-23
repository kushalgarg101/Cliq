const messages = document.querySelector('#messages');
const eventPanel = document.querySelector('#events');
const form = document.querySelector('#chat-form');
const input = document.querySelector('#message');
const sendButton = document.querySelector('#send-button');
const composerStatus = document.querySelector('#composer-status');
const appConfig = {
  csrf: document.body.dataset.csrf,
  dataReady: document.body.dataset.dataReady === 'true',
};
const headers = {'Content-Type': 'application/json', 'X-CSRF-Token': appConfig.csrf};

function addMessage(kind, text) {
  const article = document.createElement('article');
  article.className = `message ${kind}`;
  article.textContent = text;
  messages.append(article);
  messages.scrollTop = messages.scrollHeight;
}

function valueLine(label, value) {
  const line = document.createElement('p');
  line.className = 'event-detail';
  line.textContent = `${label}: ${value}`;
  return line;
}

async function readJson(response) {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return {detail: 'The server returned an unexpected response.'};
  }
}

function errorMessage(body, fallback) {
  return typeof body?.detail === 'string' ? body.detail : fallback;
}

function requireSignIn(response) {
  if (response.status !== 401) return false;
  composerStatus.textContent = 'Your session expired. Returning to sign in…';
  window.setTimeout(() => window.location.assign('/login'), 250);
  return true;
}

function actionButton(result) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = 'Confirm action';
  button.addEventListener('click', async () => {
    button.disabled = true;
    button.textContent = 'Confirming…';
    try {
      const response = await fetch(`/api/actions/${result.pending_action_id}/confirm`, {
        method: 'POST',
        headers,
      });
      const body = await readJson(response);
      if (requireSignIn(response)) return;
      addMessage('assistant', response.ok ? `Action confirmed: ${body.action_id}` : errorMessage(body, 'Action confirmation failed.'));
      if (response.ok) {
        button.textContent = 'Confirmed';
      } else {
        button.disabled = false;
        button.textContent = 'Try again';
      }
    } catch {
      addMessage('assistant', 'Network error while confirming the action. No action was confirmed.');
      button.disabled = false;
      button.textContent = 'Try again';
    }
  });
  return button;
}

function renderToolEvent(event) {
  const card = document.createElement('article');
  card.className = 'event';
  const title = document.createElement('h3');
  title.textContent = String(event.tool || 'tool').replaceAll('_', ' ');
  card.append(title);
  if (event.error) card.append(valueLine('Status', event.error));
  const result = event.result;
  if (result?.error) card.append(valueLine('Status', result.error));
  if (result?.sources) {
    for (const source of result.sources) {
      card.append(valueLine('Source', `${source.filename} · p.${source.page}`));
      if (source.excerpt) card.append(valueLine('Excerpt', source.excerpt));
    }
  }
  if (result?.order) card.append(valueLine('Record', result.order.order_id));
  if (result?.ticket) card.append(valueLine('Record', result.ticket.ticket_id));
  if (result?.cancellation) card.append(valueLine('Cancellation', result.cancellation.outcome));
  if (result?.service_credit) card.append(valueLine('Service credit', result.service_credit.outcome));
  if (result?.triage) card.append(valueLine('Triage', `${result.triage.severity} · ${result.triage.response_target}`));
  if (result?.signals) card.append(valueLine('Signals', `${result.signals.length} identified`));
  if (result?.requires_confirmation) {
    card.append(valueLine('Pending action', result.action_type.replaceAll('_', ' ')));
    if (result.payload?.reason) card.append(valueLine('Reason', result.payload.reason));
    card.append(valueLine('Confirmation', 'Select Confirm action to record this request.'));
    card.append(actionButton(result));
  }
  return card;
}

function showEvents(events) {
  eventPanel.replaceChildren();
  if (!events.length) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = 'No data tools were used.';
    eventPanel.append(empty);
    return;
  }
  events.forEach(event => eventPanel.append(renderToolEvent(event)));
}

form?.addEventListener('submit', async event => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text || !appConfig.dataReady) return;
  addMessage('user', text);
  input.value = '';
  input.disabled = true;
  sendButton.disabled = true;
  composerStatus.textContent = 'Checking authorised records…';
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers,
      body: JSON.stringify({message: text}),
    });
    const body = await readJson(response);
    if (requireSignIn(response)) return;
    addMessage(
      'assistant',
      response.ok && typeof body.answer === 'string'
        ? body.answer
        : errorMessage(body, 'The request could not be completed.'),
    );
    showEvents(body.events || []);
  } catch {
    addMessage('assistant', 'Network error. No action was performed. Please retry.');
  } finally {
    input.disabled = false;
    sendButton.disabled = false;
    composerStatus.textContent = '';
    input.focus();
  }
});

document.querySelector('#load-insights')?.addEventListener('click', async () => {
  const panel = document.querySelector('#insights');
  const button = document.querySelector('#load-insights');
  button.disabled = true;
  panel.textContent = 'Loading signals…';
  try {
    const response = await fetch('/api/insights');
    const body = await readJson(response);
    if (requireSignIn(response)) return;
    panel.replaceChildren();
    if (!response.ok) {
      panel.textContent = errorMessage(body, 'Unable to load signals.');
      return;
    }
    const signals = Array.isArray(body.signals) ? body.signals : [];
    signals.forEach(signal => {
      const card = document.createElement('article');
      const severity = String(signal.severity || 'Signal');
      const reference = signal.ticket_id || signal.order_id || signal.label || signal.ticket_ids?.join(', ') || 'Review required';
      card.className = `signal ${severity.toLowerCase()}`;
      card.textContent = `${severity} · ${reference}\n${signal.reason || 'No explanation was supplied.'}`;
      panel.append(card);
    });
    if (!signals.length) panel.textContent = 'No current signals found.';
  } catch {
    panel.textContent = 'Network error while loading signals.';
  } finally {
    button.disabled = false;
  }
});
