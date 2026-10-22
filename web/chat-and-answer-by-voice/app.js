const $ = id => document.getElementById(id);

const speechConfig = {
  timeslice: 250,
  mimeTypeCandidates: ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'],
  vadWarmupMs: 400,
  marginDb: 10,
  hysteresisDb: 4,
  silenceBoostAfterMs: 20000,
  silenceBoostDb: 6,
  speechConfirmMs: 300,
  minUtteranceMs: 650,
  maxUtteranceMs: 15000,
  hangAfterSpeechMs: 650,
};

let socket = null;
let busy = false;
let transcript = [];
let stream = null;
let recorder = null;
let audioCtx = null;
let analyser = null;
let rafId = null;
let chunks = [];
let startedAt = 0;
let lastSpeechAt = 0;
let warmupEnd = 0;
let noiseFloorDb = -55;
let inSpeech = false;
let speechCandidateSince = 0;
let keepListening = false;
let stopping = false;
let lastReplyAudioUrl = '';

function setStatus(text) { $('voice-status').textContent = text; }
function setSpeakStatus(text) { $('speak-status').textContent = text; }
function setHealth(text) { $('health').textContent = text; }
function setMeter(db) {
  const percent = Math.max(0, Math.min(100, ((db + 80) / 80) * 100));
  $('meter-fill').style.width = `${percent}%`;
  $('meter-label').textContent = `${Math.round(db)} dB`;
}
function scrollTranscriptIntoView() {
  $('heard')?.scrollIntoView({ behavior: 'smooth', block: 'end' });
}
function updateButtons(listening) {
  $('start').disabled = listening;
  $('start-hero').disabled = listening;
  $('stop').disabled = !listening;
}
function bubble(role, text) {
  $('welcome')?.remove();
  const card = document.createElement('article');
  card.className = `message ${role}`;
  const label = document.createElement('div');
  label.className = 'label';
  label.textContent = role === 'user' ? 'YOU' : 'KNOWLEDGE ASSISTANT';
  const body = document.createElement('div');
  body.className = 'body';
  body.textContent = text;
  card.append(label, body);
  $('messages').append(card);
  $('messages').scrollTop = $('messages').scrollHeight;
  return { card, body };
}
function thinkingIndicator() {
  const wrap = document.createElement('div');
  wrap.className = 'thinking-indicator';
  for (let i = 0; i < 3; i += 1) {
    const dot = document.createElement('span');
    dot.className = 'thinking-dot';
    dot.style.animationDelay = `${i * 0.15}s`;
    wrap.append(dot);
  }
  const text = document.createElement('span');
  text.textContent = 'Thinking…';
  wrap.append(text);
  return wrap;
}
function addSources(card, sources) {
  if (!sources.length) return;
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = `${sources.length} retrieved excerpts · inspect sources`;
  details.append(summary);
  for (const s of sources) {
    const item = document.createElement('section');
    item.className = 'source';
    const title = document.createElement('strong');
    title.textContent = `[${s.id}] ${s.question}`;
    const text = document.createElement('p');
    text.textContent = s.text;
    const meta = document.createElement('small');
    meta.textContent = `${s.source || 'Imported data'} · similarity ${s.score}`;
    item.append(title, text, meta);
    try {
      const url = new URL(s.url);
      if (['https:', 'http:'].includes(url.protocol)) {
        const link = document.createElement('a');
        link.href = url.href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = 'Open original source ↗';
        item.append(link);
      }
    } catch (_) {
      // No public URL for local data.
    }
    details.append(item);
  }
  card.append(details);
}
async function loadTopics() {
  try {
    const response = await fetch('/api/topics');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Unable to load topics');
    const current = $('topic').value;
    $('topic').replaceChildren();
    for (const name of data.topics) {
      const option = document.createElement('option');
      option.value = name;
      option.textContent = name.replaceAll('_', ' ');
      $('topic').append(option);
    }
    if (data.topics.includes(current)) $('topic').value = current;
    if (!data.topics.length) setStatus('No collections yet. Import data, then refresh.');
  } catch (error) {
    setHealth(error.message);
  }
}
async function refreshHealth() {
  try {
    const response = await fetch('/api/health');
    const data = await response.json();
    const voiceState = data.checks?.piper === 'ok' ? 'Amy voice ready' : 'Amy voice unavailable';
    $('health').textContent = data.ok
      ? `● Services ready · ${data.chat_model} · ${voiceState}`
      : '○ Services need attention — see /api/health';
  } catch (error) {
    setHealth(error.message);
  }
}
function clearConversation() {
  transcript = [];
  $('messages').replaceChildren();
  $('heard').textContent = 'No speech captured yet.';
  setStatus('Ready. Press start and ask a question.');
  setSpeakStatus('Ready to speak replies.');
  $('replay-answer').disabled = true;
  if (lastReplyAudioUrl) {
    URL.revokeObjectURL(lastReplyAudioUrl);
    lastReplyAudioUrl = '';
  }
  $('reply-audio').removeAttribute('src');
}
function ensureSelectedTopic() {
  const topicSelect = $('topic');
  const options = Array.from(topicSelect.options)
    .map(option => option.value)
    .filter(value => value && value !== 'Loading…');
  if (options.length && !options.includes(topicSelect.value)) {
    topicSelect.value = options[0];
  }
  return options.includes(topicSelect.value);
}
function getWsUrl() {
  const url = new URL('/ws/chat', location.href);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url;
}
function pickMimeType() {
  return speechConfig.mimeTypeCandidates.find(candidate => MediaRecorder.isTypeSupported(candidate)) || '';
}
function readLevel() {
  const buffer = new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(buffer);
  let sum = 0;
  for (const value of buffer) sum += value * value;
  const rms = Math.sqrt(sum / buffer.length) || 1e-7;
  return 20 * Math.log10(rms);
}
function stopLoop() {
  if (rafId) cancelAnimationFrame(rafId);
  rafId = null;
}
async function cleanupCapture() {
  stopLoop();
  if (audioCtx) {
    try { await audioCtx.close(); } catch (_) {}
  }
  audioCtx = null;
  analyser = null;
  if (stream) {
    for (const track of stream.getTracks()) track.stop();
  }
  stream = null;
  recorder = null;
  chunks = [];
  inSpeech = false;
  speechCandidateSince = 0;
  updateButtons(false);
}
async function pauseCaptureForPlayback() {
  const activeRecorder = recorder;
  if (!activeRecorder || activeRecorder.state === 'inactive') {
    await cleanupCapture();
    return;
  }
  stopLoop();
  await new Promise(resolve => {
    activeRecorder.onstop = async () => {
      await cleanupCapture();
      resolve();
    };
    try {
      activeRecorder.stop();
    } catch (_) {
      resolve();
    }
  });
}
async function askQuestion(question) {
  if (!ensureSelectedTopic()) {
    throw new Error('Select a knowledge collection first, then ask again.');
  }
  bubble('user', question);
  const answer = bubble('assistant', '');
  answer.body.classList.add('thinking');
  answer.body.append(thinkingIndicator());
  const record = { question, answer: '', sources: [], topic: $('topic').value, complete: false, error: '' };
  transcript.push(record);
  busy = true;
  setStatus('Connecting to chat…');
  const ws = new WebSocket(getWsUrl());
  socket = ws;
  return await new Promise((resolve, reject) => {
    let settled = false;
    const done = () => {
      if (!settled) {
        settled = true;
        resolve(record.answer);
      }
    };
    const fail = error => {
      if (!settled) {
        settled = true;
        reject(error);
      }
    };
    ws.onopen = () => ws.send(JSON.stringify({ topic: record.topic, question }));
    ws.onmessage = event => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'token') {
          if (!record.answer) {
            answer.body.classList.remove('thinking');
            answer.body.replaceChildren();
          }
          record.answer += data.text;
          answer.body.textContent = record.answer;
          $('messages').scrollTop = $('messages').scrollHeight;
        }
        if (data.type === 'sources') {
          record.sources = data.sources;
          addSources(answer.card, data.sources);
        }
        if (data.type === 'status') setStatus(data.message);
        if (data.type === 'error') {
          record.error = data.message;
          setStatus(data.message);
          if (!record.answer) {
            answer.body.classList.remove('thinking');
            answer.body.textContent = data.message;
          }
        }
        if (data.type === 'done') {
          answer.body.classList.remove('thinking');
          record.complete = true;
          setStatus('Ready for the next question.');
          busy = false;
          done();
        }
      } catch (_) {
        busy = false;
        setStatus('Received an invalid server response.');
        ws.close();
        fail(new Error('Received an invalid server response.'));
      }
    };
    ws.onerror = () => {
      busy = false;
      fail(new Error('Connection failed. Check the backend terminal.'));
    };
    ws.onclose = () => {
      if (socket === ws) socket = null;
      if (!settled) {
        busy = false;
        fail(new Error(record.error || 'Response interrupted.'));
      }
    };
  });
}
async function speakAnswer(text) {
  const cleaned = text.trim();
  if (!cleaned) return;
  setSpeakStatus('Preparing spoken reply…');
  const response = await fetch('/api/speak', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({text: cleaned}),
  });
  const audio = await response.blob();
  if (!response.ok) {
    const detail = await audio.text().catch(() => 'Unable to synthesize speech');
    throw new Error(detail || 'Unable to synthesize speech');
  }
  if (lastReplyAudioUrl) URL.revokeObjectURL(lastReplyAudioUrl);
  lastReplyAudioUrl = URL.createObjectURL(audio);
  const player = $('reply-audio');
  player.src = lastReplyAudioUrl;
  $('replay-answer').disabled = false;
  setSpeakStatus('Mic paused during playback.');
  await pauseCaptureForPlayback();
  await new Promise((resolve, reject) => {
    const finish = () => {
      player.onended = null;
      player.onerror = null;
    };
    player.onended = () => {
      finish();
      resolve();
    };
    player.onerror = () => {
      finish();
      reject(new Error('Unable to play spoken reply.'));
    };
    player.play().catch(error => {
      finish();
      reject(error);
    });
  });
  setSpeakStatus('Reply played.');
}
async function playReplyAudio({ resumeListening = false } = {}) {
  const player = $('reply-audio');
  if (!player.src) return;
  const previousSpeakStatus = $('speak-status').textContent;
  if (resumeListening) {
    setSpeakStatus('Mic paused during playback.');
    await pauseCaptureForPlayback();
  }
  player.onended = async () => {
    player.onended = null;
    if (resumeListening) {
      await startListening(false);
    }
    setSpeakStatus(previousSpeakStatus || 'Reply played.');
  };
  try {
    await player.play();
    setSpeakStatus('Reply played.');
  } catch (error) {
    player.onended = null;
    throw error;
  }
}
async function transcribeBlob(blob) {
  const formData = new FormData();
  formData.append('audio', blob, `voice.${blob.type.includes('webm') ? 'webm' : 'm4a'}`);
  const response = await fetch('/api/transcribe', { method: 'POST', body: formData });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || 'Unable to transcribe audio');
  return data;
}
async function handleCapturedAudio(blob) {
  if (!blob || blob.size === 0) {
    setStatus('No audio captured. Try again.');
    return;
  }
  try {
    setStatus('Transcribing audio…');
    const data = await transcribeBlob(blob);
    const text = String(data.text || '').trim();
    $('heard').textContent = text || 'ASR returned no text.';
    scrollTranscriptIntoView();
    if (!text) {
      setStatus('The transcription was empty.');
      return;
    }
    const answer = await askQuestion(text);
    try {
      await speakAnswer(answer);
    } catch (error) {
      setSpeakStatus(error.message);
    }
  } catch (error) {
    setStatus(error.message);
  }
}
async function stopListening(manual = false) {
  if (stopping) return;
  stopping = true;
  keepListening = !manual;
  stopLoop();
  const activeRecorder = recorder;
  if (activeRecorder && activeRecorder.state !== 'inactive') {
    await new Promise(resolve => {
      activeRecorder.onstop = async () => {
        const blob = new Blob(chunks, { type: activeRecorder.mimeType || 'audio/webm' });
        await cleanupCapture();
        await handleCapturedAudio(blob);
        resolve();
      };
      try {
        activeRecorder.stop();
      } catch (_) {
        resolve();
      }
    });
  } else {
    await cleanupCapture();
  }
  stopping = false;
  if (keepListening && !manual) {
    await startListening(false);
  }
}
async function startListening(manual = true) {
  if (stream || recorder || busy) return;
  try {
    keepListening = manual ? true : keepListening;
    chunks = [];
    inSpeech = false;
    noiseFloorDb = -55;
    startedAt = performance.now();
    lastSpeechAt = startedAt;
    warmupEnd = startedAt + speechConfig.vadWarmupMs;
    setStatus('Requesting microphone access…');
    updateButtons(true);
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, noiseSuppression: true, echoCancellation: true },
    });
    audioCtx = new AudioContext();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 2048;
    const source = audioCtx.createMediaStreamSource(stream);
    source.connect(analyser);
    const mimeType = pickMimeType();
    recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    recorder.ondataavailable = event => {
      if (event.data.size) chunks.push(event.data);
    };
    recorder.start(speechConfig.timeslice);
    setStatus('Listening. Speak, then pause.');
    const loop = () => {
      if (!analyser || !recorder || recorder.state !== 'recording') return;
      const db = readLevel();
      setMeter(db);
      const now = performance.now();
      const alpha = now < warmupEnd ? 0.25 : 0.05;
      noiseFloorDb = noiseFloorDb + (db - noiseFloorDb) * alpha;
      const quietFor = now - lastSpeechAt;
      const adaptiveBoost = quietFor >= speechConfig.silenceBoostAfterMs ? speechConfig.silenceBoostDb : 0;
      const onThresh = noiseFloorDb + speechConfig.marginDb + adaptiveBoost;
      const offThresh = onThresh - speechConfig.hysteresisDb;
      if (!inSpeech && db > onThresh) {
        if (!speechCandidateSince) speechCandidateSince = now;
        if (now - speechCandidateSince >= speechConfig.speechConfirmMs) {
          inSpeech = true;
          lastSpeechAt = now;
          speechCandidateSince = 0;
          setStatus('Speech detected…');
        }
      } else if (!inSpeech) {
        speechCandidateSince = 0;
      } else if (inSpeech && db >= offThresh) {
        lastSpeechAt = now;
      }
      if (inSpeech && now - lastSpeechAt > speechConfig.hangAfterSpeechMs) {
        void stopListening(false);
        return;
      }
      if (now - startedAt > speechConfig.maxUtteranceMs) {
        void stopListening(false);
        return;
      }
      rafId = requestAnimationFrame(loop);
    };
    rafId = requestAnimationFrame(loop);
  } catch (error) {
    updateButtons(false);
    setStatus(error.message || 'Failed to start voice capture');
    await cleanupCapture();
  }
}

$('start').onclick = () => startListening(true);
$('start-hero').onclick = () => startListening(true);
$('stop').onclick = () => stopListening(true);
$('clear').onclick = clearConversation;
$('topic').onchange = clearConversation;
$('refresh').onclick = async () => { await loadTopics(); await refreshHealth(); };
$('download').onclick = () => {
  const blob = new Blob([JSON.stringify(transcript, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = 'chat-and-answer-by-voice.json';
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$('replay-answer').onclick = async () => {
  try {
    await playReplyAudio({ resumeListening: Boolean(stream || recorder) });
  } catch (error) {
    setSpeakStatus(error.message);
  }
};
$('back-chat').onclick = () => { location.href = '/'; };

if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
  setStatus('This browser does not support microphone recording.');
  $('start').disabled = true;
  $('start-hero').disabled = true;
}

loadTopics();
refreshHealth();
setStatus('Ready. Select a topic and press start.');
setSpeakStatus('Ready to speak replies.');