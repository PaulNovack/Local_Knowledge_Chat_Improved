(() => {
  const stylesheet = document.querySelector('link[rel="stylesheet"][href="/style.css"]');
  if (stylesheet) stylesheet.href = '/style.css?v=20261022-step-video-shared';

  const numberWords = {
    one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9,
    ten: 10, eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15,
    sixteen: 16, seventeen: 17, eighteen: 18, nineteen: 19, twenty: 20, thirty: 30,
  };

  function parseStep(question) {
    const match = String(question).trim().match(/^show\s+me\s+(?:this\s+)?step\s+(\d+|[a-z]+(?:[ -]+[a-z]+)*?)\s*(?:(?:of|for)\s+(?:the\s+)?pre[ -]?trip(?:ping)?\s+inspection)?[.!?]*$/i);
    if (!match) return null;
    const phrase = match[1].toLowerCase().replace(/-/g, ' ').trim();
    let step = /^\d+$/.test(phrase) ? Number(phrase) : numberWords[phrase];
    if (!step && /^(twenty|thirty) (one|two|three|four|five|six|seven|eight|nine)$/.test(phrase)) {
      const [tens, units] = phrase.split(' ');
      step = numberWords[tens] + numberWords[units];
    }
    return Number.isInteger(step) && step >= 1 && step <= 39 ? step : null;
  }

  function parseVideoRequest(question) {
    const step = parseStep(question);
    if (step) return {id: `step/${step}`, label: `Step ${step} pre-trip inspection video`};
    if (/^show\s+me\s+the\s+complete\s+pre[ -]?trip\s+inspection[.!?]*$/i.test(String(question).trim())) {
      return {
        steps: Array.from({length: 39}, (_, index) => index + 1),
        viewUrl: '/api/videos/pre-trip/complete/view',
        label: 'Complete pre-trip inspection video sequence',
      };
    }
    if (/^show\s+me\s+how\s+to\s+(?:hook[ -]?up|connect)\s+(?:(?:a|the)\s+)?trailer[.!?]*$/i.test(String(question).trim())) {
      return {id: 'named/fifth-wheel-coupling', label: 'Fifth-wheel coupling walkthrough'};
    }
    if (/^show\s+me\s+how\s+to\s+(?:unhook(?:[ -]?up)?|disconnect)\s+(?:(?:a|the)\s+)?trailer[.!?]*$/i.test(String(question).trim())) {
      return {id: 'named/fifth-wheel-uncoupling', label: 'Fifth-wheel uncoupling walkthrough'};
    }
    if (/^show(?:\s+me)?\s+(?:making\s+(?:a\s+)?(?:lane|lion)\s+change|(?:how\s+to\s+)?change\s+lanes?)[.!?]*$/i.test(String(question).trim())) {
      return {id: 'named/lane-change', label: 'Lane change check'};
    }
    if (/^show(?:\s+me)?\s+making\s+a\s+right\s+turn[.!?]*$/i.test(String(question).trim())) {
      return {id: 'named/right-turn', label: 'Right turn tractor-trailer walkthrough'};
    }
    return null;
  }

  function setStatus(message) {
    const status = document.getElementById('status');
    if (status) status.textContent = message;
  }

  function showVideo(videoRequest, question) {
    const overlay = document.createElement('section');
    overlay.className = 'video-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', videoRequest.label);

    const closeButton = document.createElement('button');
    closeButton.className = 'video-overlay-close';
    closeButton.type = 'button';
    closeButton.textContent = 'Close video';

    const video = document.createElement('video');
    video.controls = true;
    video.autoplay = false;
    video.playsInline = true;
    video.preload = 'auto';
    let currentStepIndex = 0;
    const videoUrl = videoRequest.steps
      ? `/api/videos/step/${videoRequest.steps[currentStepIndex]}`
      : `/api/videos/${videoRequest.id}`;
    const viewUrl = videoRequest.viewUrl || `${videoUrl}/view`;
    overlay.append(closeButton, video);
    document.body.append(overlay);
    document.body.classList.add('video-overlay-open');

    let closed = false;
  let microphonePause = Promise.resolve();
    const onKeydown = event => {
      if (event.key === 'Escape') close('Video closed.');
    };
    const close = message => {
      if (closed) return;
      closed = true;
      video.pause();
      video.removeAttribute('src');
      video.load();
      overlay.remove();
      document.body.classList.remove('video-overlay-open');
      document.removeEventListener('keydown', onKeydown);
      setStatus(message);
      document.getElementById('question')?.focus();
      void microphonePause
        .catch(() => {})
        .then(() => window.resumeMicrophoneAfterVideo?.())
        .catch(() => setStatus('Unable to resume microphone listening.'));
    };

    closeButton.addEventListener('click', () => close('Video closed.'));
    fetch(viewUrl, {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({question}),
        })
      .then(response => {
        if (!response.ok) throw new Error('Unable to add video to Responses.');
      })
      .catch(error => setStatus(error.message || 'Unable to add video to Responses.'));
    video.addEventListener('ended', () => {
      if (!videoRequest.steps || currentStepIndex === videoRequest.steps.length - 1) {
        close('Video finished.');
        return;
      }
      currentStepIndex += 1;
      const nextStep = videoRequest.steps[currentStepIndex];
      video.setAttribute('aria-label', `Step ${nextStep} of ${videoRequest.steps.length}`);
      video.src = `/api/videos/step/${nextStep}`;
      video.play().catch(() => setStatus(`Step ${nextStep} is ready. Press Play to continue.`));
    });
    video.addEventListener('error', () => {
      const label = videoRequest.steps
        ? `Step ${videoRequest.steps[currentStepIndex]} could not be loaded.`
        : `${videoRequest.label} could not be loaded.`;
      close(label);
    });
    document.addEventListener('keydown', onKeydown);
    closeButton.focus();
    try {
      microphonePause = Promise.resolve(window.pauseMicrophoneForVideo?.());
    } catch (error) {
      microphonePause = Promise.reject(error);
    }
    microphonePause.then(() => {
      if (closed) return;
      video.src = videoUrl;
      video.play().catch(() => setStatus('Video is ready. Press Play to start it.'));
    }).catch(error => {
      setStatus(error.message || 'Microphone could not be paused; video playback was not started.');
      close('Video playback was blocked because the microphone could not be paused.');
    });
  }

  window.handleStepVideoRequest = question => {
    const videoRequest = parseVideoRequest(question);
    if (!videoRequest) return false;
    showVideo(videoRequest, String(question).trim());
    return true;
  };
})();