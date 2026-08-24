const messages = document.querySelector('#messages');
const eventPanel = document.querySelector('#events');
const eventSummary = document.querySelector('#events-summary');
const form = document.querySelector('#chat-form');
const input = document.querySelector('#message');
const sendButton = document.querySelector('#send-button');
const composerStatus = document.querySelector('#composer-status');
const appConfig = {
  csrf: document.body.dataset.csrf,
  dataReady: document.body.dataset.dataReady === 'true',
};
const headers = {'Content-Type': 'application/json', 'X-CSRF-Token': appConfig.csrf};

function appendInlineText(parent, value) {
  const parts = String(value).split(/(\*\*[^*\n]+\*\*)/g);
  for (const part of parts) {
    const match = part.match(/^\*\*([^*\n]+)\*\*$/);
    if (match) {
      const strong = document.createElement('strong');
      strong.textContent = match[1];
      parent.append(strong);
    } else {
      parent.append(document.createTextNode(part));
    }
  }
}

function tableCells(line) {
  return line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
}

function isTableDivider(line) {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
}

function appendTable(parent, header, rows) {
  const wrap = document.createElement('div');
  wrap.className = 'md-table-wrap';
  const table = document.createElement('table');
  table.className = 'md-table';
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  for (const cell of header) {
    const th = document.createElement('th');
    th.scope = 'col';
    appendInlineText(th, cell);
    headRow.append(th);
  }
  thead.append(headRow);
  table.append(thead);
  const tbody = document.createElement('tbody');
  for (const row of rows) {
    const tr = document.createElement('tr');
    for (let index = 0; index < header.length; index += 1) {
      const td = document.createElement('td');
      appendInlineText(td, row[index] || '');
      tr.append(td);
    }
    tbody.append(tr);
  }
  table.append(tbody);
  wrap.append(table);
  parent.append(wrap);
}

function renderAssistantMessage(article, text) {
  const lines = String(text).replace(/\r\n/g, '\n').split('\n');
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    if (/^\s*\|.+\|\s*$/.test(line) && isTableDivider(lines[index + 1] || '')) {
      const header = tableCells(line);
      const rows = [];
      index += 2;
      while (index < lines.length && /^\s*\|.+\|\s*$/.test(lines[index])) rows.push(tableCells(lines[index++]));
      appendTable(article, header, rows);
      continue;
    }
    const heading = line.match(/^\s{0,3}#{1,3}\s+(.+)$/);
    const boldHeading = line.match(/^\s*\*\*([^*]+)\*\*\s*$/);
    if (heading || boldHeading) {
      const title = document.createElement('h3');
      appendInlineText(title, (heading || boldHeading)[1]);
      article.append(title);
      index += 1;
      continue;
    }
    if (/^\s*(?:---|\*\*\*|___)\s*$/.test(line)) {
      article.append(document.createElement('hr'));
      index += 1;
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const list = document.createElement('ul');
      while (index < lines.length) {
        const item = lines[index].match(/^\s*[-*]\s+(.+)$/);
        if (!item) break;
        const li = document.createElement('li');
        appendInlineText(li, item[1]);
        list.append(li);
        index += 1;
      }
      article.append(list);
      continue;
    }
    const paragraph = [];
    while (index < lines.length && lines[index].trim()
      && !/^\s{0,3}#{1,3}\s+/.test(lines[index])
      && !/^\s*\*\*[^*]+\*\*\s*$/.test(lines[index])
      && !/^\s*(?:---|\*\*\*|___)\s*$/.test(lines[index])
      && !/^\s*[-*]\s+/.test(lines[index])
      && !(/^\s*\|.+\|\s*$/.test(lines[index]) && isTableDivider(lines[index + 1] || ''))) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    const element = document.createElement('p');
    appendInlineText(element, paragraph.join(' '));
    article.append(element);
  }
  if (!article.childNodes.length) article.textContent = String(text);
}

function scrollMessagesToEnd() {
  scrollMessagesToEnd();
}

function toolProgressLabel(event) {
  const result = event.result || {};
  if (event.tool === 'evaluate_order') return `Checked order ${result.order?.order_id || 'record'} against applicable rules`;
  if (event.tool === 'evaluate_ticket') return `Assessed ticket ${result.ticket?.ticket_id || 'record'} priority and SLA target`;
  if (event.tool === 'search_knowledge') return `Reviewed ${result.sources?.length || 0} current authorised source${result.sources?.length === 1 ? '' : 's'}`;
  if (event.tool === 'lookup_operations') return 'Retrieved authorised operational records';
  if (event.tool === 'prepare_action') return 'Prepared a confirmation-gated action draft';
  if (event.tool === 'scan_issue_signals') return 'Scanned support activity for priority signals';
  return String(event.tool || 'Completed an authorised tool step').replaceAll('_', ' ');
}

function startActivity() {
  const article = document.createElement('article');
  article.className = 'activity-message';
  const header = document.createElement('div');
  header.className = 'activity-header';
  const pulse = document.createElement('span');
  pulse.className = 'activity-pulse';
  const title = document.createElement('span');
  title.textContent = 'Working on your request';
  header.append(pulse, title);
  const rows = document.createElement('div');
  rows.className = 'activity-rows';
  article.append(header, rows);
  messages.append(article);
  scrollMessagesToEnd();

  const stages = [
    'Validating your session and access scope',
    'Preparing an authorised evidence request',
    'Waiting for a grounded response',
  ];
  let stage = 0;
  const renderStage = () => {
    rows.replaceChildren();
    const row = document.createElement('p');
    row.className = 'activity-row pending';
    row.textContent = stages[stage];
    rows.append(row);
    composerStatus.textContent = stages[stage];
  };
  renderStage();
  const timer = window.setInterval(() => {
    stage = Math.min(stage + 1, stages.length - 1);
    renderStage();
  }, 850);
  return {article, rows, timer};
}

function finishActivity(activity, events, failed = false) {
  window.clearInterval(activity.timer);
  const title = activity.article.querySelector('.activity-header span:last-child');
  title.textContent = failed ? 'Request could not be completed' : events.length ? `${events.length} tool step${events.length === 1 ? '' : 's'} completed` : 'Response completed';
  activity.article.classList.toggle('failed', failed);
  activity.rows.replaceChildren();
  if (!events.length) {
    const row = document.createElement('p');
    row.className = `activity-row ${failed ? 'failed' : 'complete'}`;
    row.textContent = failed ? 'No action was performed.' : 'No data tools were needed.';
    activity.rows.append(row);
  } else {
    for (const event of events) {
      const row = document.createElement('p');
      row.className = 'activity-row complete';
      row.textContent = toolProgressLabel(event);
      activity.rows.append(row);
    }
  }
  scrollMessagesToEnd();
}
function addMessage(kind, text, hasEvidence = false) {
  const article = document.createElement('article');
  article.className = `message ${kind}`;
  if (kind === 'assistant') renderAssistantMessage(article, text);
  else article.textContent = text;
  if (kind === 'assistant' && hasEvidence) {
    const note = document.createElement('div');
    note.className = 'message-note';
    note.textContent = 'Supporting sources and tool steps are available in Evidence and tools.';
    article.append(note);
  }
  messages.append(article);
  scrollMessagesToEnd();
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

function valueLine(label, value) {
  const line = document.createElement('p');
  line.className = 'event-detail';
  const strong = document.createElement('strong');
  strong.textContent = `${label}: `;
  line.append(strong, document.createTextNode(String(value)));
  return line;
}

function sourceDetail(source) {
  const detail = document.createElement('details');
  detail.className = 'source-detail';
  const summary = document.createElement('summary');
  summary.textContent = `${source.filename} · page ${source.page}`;
  detail.append(summary);
  if (source.excerpt) {
    const excerpt = document.createElement('p');
    excerpt.className = 'source-excerpt';
    excerpt.textContent = source.excerpt;
    detail.append(excerpt);
  }
  return detail;
}

function renderToolEvent(event) {
  const card = document.createElement('article');
  card.className = 'event';
  const tool = String(event.tool || 'tool');
  const heading = document.createElement('div');
  heading.className = 'event-header';
  const icon = document.createElement('span');
  icon.className = 'tool-icon';
  icon.textContent = tool === 'search_knowledge' ? 'DOC' : tool === 'evaluate_order' ? 'ORD' : tool === 'evaluate_ticket' ? 'TKT' : tool === 'prepare_action' ? 'ACT' : 'RUN';
  const title = document.createElement('h3');
  title.textContent = tool.replaceAll('_', ' ');
  heading.append(icon, title);
  card.append(heading);
  const body = document.createElement('div');
  body.className = 'event-body';
  if (event.error) body.append(valueLine('Status', event.error));
  const result = event.result || {};
  if (result.error) body.append(valueLine('Status', result.error));
  if (result.order) body.append(valueLine('Order', result.order.order_id));
  if (result.ticket) body.append(valueLine('Ticket', result.ticket.ticket_id));
  if (result.cancellation) body.append(valueLine('Cancellation', result.cancellation.outcome));
  if (result.service_credit) body.append(valueLine('Service credit', result.service_credit.outcome));
  if (result.triage) body.append(valueLine('Triage', `${result.triage.severity} · ${result.triage.response_target}`));
  if (result.signals) body.append(valueLine('Signals', `${result.signals.length} identified`));
  if (result.sources) {
    for (const source of result.sources) body.append(sourceDetail(source));
  }
  if (result.requires_confirmation) {
    body.append(valueLine('Pending action', result.action_type.replaceAll('_', ' ')));
    if (result.payload?.reason) body.append(valueLine('Reason', result.payload.reason));
    body.append(valueLine('Confirmation', 'Select Confirm action to record this request.'));
    body.append(actionButton(result));
  }
  card.append(body);
  return card;
}

function showEvents(events) {
  eventPanel.replaceChildren();
  if (eventSummary) eventSummary.textContent = events.length ? `${events.length} step${events.length === 1 ? '' : 's'}` : 'No tools';
  if (!events.length) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = 'No data tools were needed for this response.';
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
  const activity = startActivity();
  input.value = '';
  input.disabled = true;
  sendButton.disabled = true;
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers,
      body: JSON.stringify({message: text}),
    });
    const body = await readJson(response);
    if (requireSignIn(response)) return;
    const events = Array.isArray(body.events) ? body.events : [];
    addMessage(
      'assistant',
      response.ok && typeof body.answer === 'string'
        ? body.answer
        : errorMessage(body, 'The request could not be completed.'),
      response.ok && events.length > 0,
    );
    finishActivity(activity, events);
    showEvents(events);
  } catch {
    finishActivity(activity, [], true);
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
