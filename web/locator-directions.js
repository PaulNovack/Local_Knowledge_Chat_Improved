(() => {
  const mapOpenDelayMs = 1000;

  window.isLocatorDirectionsRequest = question =>
    /^(?:open|show)(?: me)?(?: the)? directions?$/i.test(
      String(question || '').replace(/[.!?,]/g, '').replace(/\s+/g, ' ').trim(),
    );

  function directionsUrl(destination) {
    const address = String(destination.address || '').trim();
    const hasCoordinates = destination.latitude != null && destination.longitude != null
      && destination.latitude !== '' && destination.longitude !== '';
    const latitude = hasCoordinates ? Number(destination.latitude) : NaN;
    const longitude = hasCoordinates ? Number(destination.longitude) : NaN;
    const coordinates = Number.isFinite(latitude) && Number.isFinite(longitude)
      ? `${latitude},${longitude}`
      : '';
    const target = address || coordinates;
    if (!target || target.toLowerCase() === 'not listed') return null;
    const url = new URL('https://www.google.com/maps/dir/');
    url.searchParams.set('api', '1');
    url.searchParams.set('destination', target);
    return url.href;
  }

  window.prepareLocatorDirections = (question, enabled) => {
    if (!/\b(?:closest|nearest|near me|nearby)\b/i.test(question)) return null;
    const popup = enabled ? window.open('about:blank', '_blank') : null;
    if (popup) {
      popup.document.title = 'Finding directions';
      popup.document.body.textContent = 'Finding the closest location…';
      popup.opener = null;
    }
    return {popup, matched: false, finished: false, audioStartedAt: null, url: null, timer: null};
  };

  function scheduleMapOpen(controller) {
    if (!controller?.popup || controller.popup.closed || !controller.url
      || controller.audioStartedAt === null || controller.timer !== null) return;
    const elapsed = performance.now() - controller.audioStartedAt;
    const delay = Math.max(0, mapOpenDelayMs - elapsed);
    controller.timer = window.setTimeout(() => {
      controller.timer = null;
      try {
        if (!controller.popup.closed) controller.popup.location.replace(controller.url);
      } catch (_) {
        // The inline link remains available if the popup was blocked or closed.
      }
    }, delay);
  }

  window.locatorDirectionsAudioStarted = controller => {
    if (!controller || controller.audioStartedAt !== null) return;
    controller.audioStartedAt = performance.now();
    scheduleMapOpen(controller);
  };

  window.openLocatorDirections = controller => {
    if (!controller?.url) return false;
    if (controller.popup && !controller.popup.closed) {
      controller.popup.location.replace(controller.url);
      return true;
    }
    const opened = window.open(controller.url, '_blank', 'noopener,noreferrer');
    if (opened) return true;
    window.location.assign(controller.url);
    return true;
  };

  window.resolveLocatorDirections = (controller, sources, card) => {
    if (!controller || controller.matched) return;
    const destination = (Array.isArray(sources) ? sources : [])
      .map(source => source?.directions || source?.location || source)
      .find(candidate => candidate && directionsUrl(candidate));
    if (!destination) return;
    controller.matched = true;
    controller.url = directionsUrl(destination);

    if (card && !card.querySelector('.locator-directions-link')) {
      const link = document.createElement('a');
      link.className = 'locator-directions-link';
      link.href = controller.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'Open Directions';
      const answerBody = card.querySelector('.body');
      if (answerBody) answerBody.after(link);
      else card.prepend(link);
    }

    scheduleMapOpen(controller);
  };

  window.finishLocatorDirections = controller => {
    if (!controller || controller.finished) return;
    controller.finished = true;
    if (!controller.matched) controller.popup?.close();
  };
})();
