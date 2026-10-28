(() => {
  const mapOpenDelayMs = 1000;
  let directionsOverlay = null;
  let directionsFrame = null;
  let directionsFrameLoaded = false;
  let previousFocus = null;
  let previousBodyOverflow = '';
  let latestController = null;
  let latestLocation = null;
  let latestHeadingLocation = null;

  const showMapToggle = document.getElementById('show-map');
  if (showMapToggle) {
    const savedShowMap = document.cookie.match(/(?:^|;\s*)showMap=([^;]*)/);
    if (savedShowMap) showMapToggle.checked = savedShowMap[1] === '1';
    showMapToggle.addEventListener('change', () => {
      document.cookie = `showMap=${showMapToggle.checked ? '1' : '0'}; Max-Age=31536000; Path=/; SameSite=Lax`;
    });
  }

  const stateOnlyAddressValues = new Set([
    'alabama', 'alaska', 'arizona', 'arkansas', 'california', 'colorado', 'connecticut',
    'delaware', 'florida', 'georgia', 'hawaii', 'idaho', 'illinois', 'indiana', 'iowa',
    'kansas', 'kentucky', 'louisiana', 'maine', 'maryland', 'massachusetts', 'michigan',
    'minnesota', 'mississippi', 'missouri', 'montana', 'nebraska', 'nevada', 'new hampshire',
    'new jersey', 'new mexico', 'new york', 'north carolina', 'north dakota', 'ohio',
    'oklahoma', 'oregon', 'pennsylvania', 'rhode island', 'south carolina', 'south dakota',
    'tennessee', 'texas', 'utah', 'vermont', 'virginia', 'washington', 'west virginia',
    'wisconsin', 'wyoming', 'district of columbia',
    'al', 'ak', 'az', 'ar', 'ca', 'co', 'ct', 'de', 'fl', 'ga', 'hi', 'id', 'il', 'in',
    'ia', 'ks', 'ky', 'la', 'me', 'md', 'ma', 'mi', 'mn', 'ms', 'mo', 'mt', 'ne', 'nv',
    'nh', 'nj', 'nm', 'ny', 'nc', 'nd', 'oh', 'ok', 'or', 'pa', 'ri', 'sc', 'sd', 'tn',
    'tx', 'ut', 'vt', 'va', 'wa', 'wv', 'wi', 'wy', 'dc',
  ]);

  function directionsUrl(destination) {
    const address = String(destination?.address || '').trim();
    const addressWithoutCountry = address.replace(/,?\s+(?:USA|US|United States)$/i, '').trim();
    const addressIsStateOnly = stateOnlyAddressValues.has(addressWithoutCountry.toLowerCase());
    const addressIsAvailable = address && address.toLowerCase() !== 'not listed' && !addressIsStateOnly;
    const latitude = Number(destination?.latitude);
    const longitude = Number(destination?.longitude);
    const hasCoordinates = destination?.latitude != null && destination.latitude !== ''
      && destination?.longitude != null && destination.longitude !== ''
      && Number.isFinite(latitude) && Number.isFinite(longitude)
      && latitude >= -90 && latitude <= 90 && longitude >= -180 && longitude <= 180;
    const coordinates = hasCoordinates
      ? `${latitude},${longitude}`
      : '';
    const target = addressIsAvailable ? address : coordinates;
    if (!target) return null;
    const url = new URL('https://www.google.com/maps/dir/');
    url.searchParams.set('api', '1');
    url.searchParams.set('destination', target);
    url.searchParams.set('travelmode', 'driving');
    url.searchParams.set('dir_action', 'navigate');
    const cookieLocation = document.cookie.match(/(?:^|;\s*)geolocation=([^;]*)/);
    const [originLatitude, originLongitude] = cookieLocation
      ? decodeURIComponent(cookieLocation[1]).split(':')
      : [window.locatorCurrentLocation?.latitude, window.locatorCurrentLocation?.longitude];
    const originLatitudeValue = Number(originLatitude);
    const originLongitudeValue = Number(originLongitude);
    if (originLatitude != null && originLongitude != null
      && Number.isFinite(originLatitudeValue) && originLatitudeValue >= -90 && originLatitudeValue <= 90
      && Number.isFinite(originLongitudeValue) && originLongitudeValue >= -180 && originLongitudeValue <= 180) {
      url.searchParams.set('origin', `${originLatitudeValue},${originLongitudeValue}`);
    }
    return url.href;
  }

  function mapQuestUrl(destination, waitForInitialPlayback = false) {
    const routeUrl = directionsUrl(destination);
    if (!routeUrl) return null;
    const route = new URL(routeUrl);
    const url = new URL('/mapquest-directions/', window.location.origin);
    url.searchParams.set('to', route.searchParams.get('destination'));
    if (waitForInitialPlayback) url.searchParams.set('waitForPlayback', '1');
    const location = window.locatorCurrentLocation;
    const latitude = Number(location?.latitude);
    const longitude = Number(location?.longitude);
    if (location?.latitude != null && location.latitude !== ''
      && location?.longitude != null && location.longitude !== ''
      && Number.isFinite(latitude) && latitude >= -90 && latitude <= 90
      && Number.isFinite(longitude) && longitude >= -180 && longitude <= 180) {
      url.searchParams.set('from', `${latitude},${longitude}`);
    }
    return url.href;
  }

  window.prepareLocatorDirections = (question, enabled, waitForInitialPlayback = false) => {
    const listRequest = /\b(?:show|give|display|list)\b.*\b(?:list|grocery|groceries|supermarket)\b/i.test(question);
    const numberRequest = /\b(?:show|open|give)\b.*?\bdirections?\b.*?\b(?:for|to)\s+(?:number\s+|#\s*)?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b/i.test(question);
    if (!listRequest && !numberRequest
      && !/\b(?:closest|nearest|newest|near me|nearby)\b/i.test(question)) return null;
    if (latestController && latestController.timer !== null) window.clearTimeout(latestController.timer);
    latestController = {
      autoOpen: numberRequest || (!listRequest && Boolean(enabled)),
      immediateOpen: numberRequest, matched: false, finished: false,
      audioStartedAt: null, url: null, timer: null,
      waitForInitialPlayback: Boolean(waitForInitialPlayback),
      initialPlaybackFinished: false, playbackSignalSent: false,
      responseId: null, navigationData: null, navigationVersion: 0, navigationSaving: false,
    };
    return latestController;
  };

  async function saveNavigationData(controller) {
    if (!controller?.responseId || !controller.navigationData || controller.navigationSaving) return;
    const version = controller.navigationVersion;
    controller.navigationSaving = true;
    try {
      const response = await fetch(`/api/responses/${controller.responseId}/navigation`, {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify(controller.navigationData),
      });
      if (!response.ok) throw new Error('Unable to save navigation data.');
    } catch (error) {
      console.warn('Could not save route details to response history.', error);
    } finally {
      controller.navigationSaving = false;
      if (controller.navigationVersion !== version) void saveNavigationData(controller);
    }
  }

  function closeDirectionsFrame() {
    let closed = false;
    if (latestController && latestController.timer !== null) {
      window.clearTimeout(latestController.timer);
      latestController.timer = null;
      closed = true;
    }
    if (!directionsOverlay) return closed;
    directionsOverlay.remove();
    directionsOverlay = null;
    directionsFrame = null;
    directionsFrameLoaded = false;
    document.body.style.overflow = previousBodyOverflow;
    previousFocus?.focus?.();
    previousFocus = null;
    return true;
  }

  function notifyInitialPlaybackFinished() {
    const controller = latestController;
    if (!controller?.waitForInitialPlayback || !controller.initialPlaybackFinished
      || controller.playbackSignalSent || !directionsFrameLoaded || !directionsFrame?.contentWindow) return;
    directionsFrame.contentWindow.postMessage(
      {type: 'mapquest:initial-playback-finished'},
      window.location.origin,
    );
    controller.playbackSignalSent = true;
    controller.waitForInitialPlayback = false;
  }

  function openDirectionsFrame(url) {
    if (!url) return false;
    if (!directionsOverlay) {
      previousFocus = document.activeElement;
      previousBodyOverflow = document.body.style.overflow;
      directionsOverlay = document.createElement('section');
      directionsOverlay.setAttribute('role', 'dialog');
      directionsOverlay.setAttribute('aria-modal', 'true');
      directionsOverlay.setAttribute('aria-label', 'MapQuest directions');
      directionsOverlay.style.cssText = [
        'position:fixed', 'inset:0', 'z-index:2147483647', 'display:flex',
        'flex-direction:column', 'background:#fff', 'width:100vw', 'height:100vh', 'height:100dvh',
      ].join(';');

      const toolbar = document.createElement('div');
      toolbar.style.cssText = [
        'display:flex', 'justify-content:flex-end', 'align-items:center',
        'height:48px', 'min-height:48px', 'padding:0 12px',
        'background:#fff', 'border-bottom:1px solid #d7dce0',
      ].join(';');
      const closeButton = document.createElement('button');
      closeButton.type = 'button';
      closeButton.textContent = 'Close directions';
      closeButton.setAttribute('aria-label', 'Close directions');
      closeButton.addEventListener('click', closeDirectionsFrame);
      toolbar.append(closeButton);

      directionsFrame = document.createElement('iframe');
      directionsFrame.title = 'MapQuest driving directions';
      directionsFrame.allow = 'geolocation';
      directionsFrame.referrerPolicy = 'strict-origin-when-cross-origin';
      directionsFrame.style.cssText = 'width:100%;height:calc(100% - 48px);flex:1;border:0';
      directionsFrame.addEventListener('load', () => {
        directionsFrameLoaded = true;
        const location = latestHeadingLocation || latestLocation || window.locatorCurrentLocation;
        if (location && directionsFrame.contentWindow) {
          directionsFrame.contentWindow.postMessage(
            {type: 'mapquest:heading-updated', location},
            window.location.origin,
          );
        }
        notifyInitialPlaybackFinished();
      });
      directionsOverlay.append(toolbar, directionsFrame);
      document.body.append(directionsOverlay);
      document.body.style.overflow = 'hidden';
      closeButton.focus();
    }
    if (directionsFrame.src !== url) {
      directionsFrameLoaded = false;
      directionsFrame.src = url;
    }
    return true;
  }

  function scheduleMapOpen(controller) {
    if (!controller?.autoOpen || !controller.mapUrl
      || controller.audioStartedAt === null || controller.timer !== null) return;
    const elapsed = performance.now() - controller.audioStartedAt;
    const delay = Math.max(0, mapOpenDelayMs - elapsed);
    controller.timer = window.setTimeout(() => {
      controller.timer = null;
      openDirectionsFrame(controller.mapUrl);
    }, delay);
  }

  window.locatorDirectionsAudioStarted = controller => {
    if (!controller || controller.audioStartedAt !== null) return;
    controller.audioStartedAt = performance.now();
    scheduleMapOpen(controller);
  };

  window.openLocatorDirections = controller => {
    if (!controller?.destination) return false;
    controller.mapUrl = mapQuestUrl(controller.destination, controller.waitForInitialPlayback);
    return openDirectionsFrame(controller.mapUrl);
  };

  window.closeLocatorDirections = closeDirectionsFrame;

  window.locatorDirectionsInitialPlaybackFinished = controller => {
    if (!controller?.waitForInitialPlayback) return;
    controller.initialPlaybackFinished = true;
    notifyInitialPlaybackFinished();
  };

  window.addEventListener('locator-location-updated', event => {
    if (!event.detail) return;
    latestLocation = event.detail;
    if (!latestController?.destination || !directionsFrame) return;
    if (!directionsFrameLoaded || !directionsFrame.contentWindow) return;
    directionsFrame.contentWindow.postMessage(
      {type: 'mapquest:location-updated', location: event.detail},
      window.location.origin,
    );
  });

  window.addEventListener('locator-heading-updated', event => {
    if (!event.detail) return;
    latestHeadingLocation = event.detail;
    if (!directionsFrameLoaded || !directionsFrame?.contentWindow) return;
    directionsFrame.contentWindow.postMessage(
      {type: 'mapquest:heading-updated', location: event.detail},
      window.location.origin,
    );
  });

  window.addEventListener('message', event => {
    if (event.origin !== window.location.origin || event.source !== directionsFrame?.contentWindow
      || event.data?.type !== 'mapquest:navigation-snapshot' || !latestController) return;
    latestController.navigationData = event.data.navigation;
    latestController.navigationVersion += 1;
    void saveNavigationData(latestController);
  });

  window.handleLocatorDirectionsCommand = question => {
    const normalized = String(question || '').replace(/[.!?,]/g, '').replace(/\s+/g, ' ').trim();
    if (directionsOverlay) {
      if (/^close directions$/i.test(normalized)) return closeDirectionsFrame() ? 'closed' : 'not-open';
      return 'ignored';
    }
    if (/^(?:open|show)(?: me)?(?: the)? directions?$/i.test(normalized)) {
      return window.openLocatorDirections(latestController) ? 'opened' : 'no-location';
    }
    if (/^(?:close|hide)(?: the)? directions?$/i.test(normalized)) {
      return closeDirectionsFrame() ? 'closed' : 'not-open';
    }
    return null;
  };

  window.resolveLocatorDirections = (controller, sources, card) => {
    if (!controller || controller.matched) return;
    const listedDestinations = (Array.isArray(sources) ? sources : [])
      .flatMap(source => Array.isArray(source?.directionsList) ? source.directionsList : []);
    if (listedDestinations.length) {
      controller.matched = true;
      const list = document.createElement('ol');
      list.className = 'locator-directions-list';
      for (const destination of listedDestinations) {
        const url = mapQuestUrl(destination);
        const item = document.createElement('li');
        const name = document.createElement('span');
        const storeName = String(destination.name || '');
        const duplicateNumber = storeName.match(/^\s*(\d+)[.)]\s*/);
        const displayName = duplicateNumber?.[1] === String(destination.number)
          ? storeName.slice(duplicateNumber[0].length)
          : storeName;
        name.textContent = displayName;
        item.append(name);
        if (url) {
          const link = document.createElement('a');
          link.href = url;
          link.className = 'locator-directions-link';
          link.textContent = 'Open directions';
          link.addEventListener('click', event => {
            event.preventDefault();
            controller.destination = destination;
            window.openLocatorDirections(controller);
          });
          item.append(' · ', link);
        }
        list.append(item);
      }
      const answerBody = card?.querySelector('.body');
      if (answerBody) answerBody.after(list);
      else card?.append(list);
      return;
    }
    const destination = (Array.isArray(sources) ? sources : [])
      .map(source => source?.directions || source?.location || source)
      .find(candidate => candidate && directionsUrl(candidate));
    if (!destination) return;
    controller.matched = true;
    controller.destination = destination;
    controller.mapUrl = mapQuestUrl(destination, controller.waitForInitialPlayback);

    if (card && !card.querySelector('.locator-directions-link')) {
      const link = document.createElement('a');
      link.className = 'locator-directions-link';
      link.href = controller.mapUrl;
      link.textContent = 'Open MapQuest driving directions';
      link.addEventListener('click', event => {
        event.preventDefault();
        window.openLocatorDirections(controller);
      });
      const answerBody = card.querySelector('.body');
      if (answerBody) answerBody.after(link);
      else card.prepend(link);
    }

    if (controller.immediateOpen) window.openLocatorDirections(controller);
    else scheduleMapOpen(controller);
  };

  window.finishLocatorDirections = (controller, responseId) => {
    if (!controller) return;
    controller.finished = true;
    if (Number.isInteger(responseId) && responseId > 0) {
      controller.responseId = responseId;
      void saveNavigationData(controller);
    }
  };
})();
