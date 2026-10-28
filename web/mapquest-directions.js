(() => {
  const parameters = new URLSearchParams(window.location.search);
  const destination = parameters.get('to');
  const status = document.getElementById('status');
  const summary = document.getElementById('summary');
  const maneuversList = document.getElementById('maneuvers');
  const playButton = document.getElementById('play-directions');
  let playbackAllowed = parameters.get('waitForPlayback') !== '1';
  let routeLoaded = false;
  let automaticPlaybackStarted = false;
  let directionsAudio = null;
  let directionsAudioUrl = '';
  let parentMicrophonePaused = false;
  let currentDirectionsSignature = null;
  let routeRequestVersion = 0;
  let latestHeadingLocation = null;
  let lastBearingPosition = null;
  let smoothedBearing = null;

  function coordinates(value) {
    if (!value) return null;
    const parts = value.split(',').map(Number);
    if (parts.length !== 2 || !parts.every(Number.isFinite)
      || parts[0] < -90 || parts[0] > 90 || parts[1] < -180 || parts[1] > 180) return null;
    return parts;
  }

  function directionsSignature(maneuvers) {
    return JSON.stringify(maneuvers.map(maneuver => String(maneuver.narrative || '')
      .replace(/\b(?:go|continue|travel|proceed)\s+for\s+[\d,.]+\s*(?:ft\.?|feet|mi\.?|miles?|km\.?|kilometers?)\b/gi, '')
      .replace(/\s+/g, ' ')
      .trim()
      .toLowerCase()));
  }

  const map = L.map('map', {rotate: true, rotateControl: false, zoomSnap: 0.1}).setView([39.5, -98.35], 4);
  const routeLayers = L.layerGroup().addTo(map);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors',
  }).addTo(map);

  function normalizeBearing(bearing) {
    return (bearing % 360 + 360) % 360;
  }

  function distanceBetween(first, second) {
    const radians = value => value * Math.PI / 180;
    const latitudeDelta = radians(second[0] - first[0]);
    const longitudeDelta = radians(second[1] - first[1]);
    const latitudeOne = radians(first[0]);
    const latitudeTwo = radians(second[0]);
    const arc = Math.sin(latitudeDelta / 2) ** 2
      + Math.cos(latitudeOne) * Math.cos(latitudeTwo)
      * Math.sin(longitudeDelta / 2) ** 2;
    return 6371000 * 2 * Math.atan2(Math.sqrt(arc), Math.sqrt(1 - arc));
  }

  function bearingBetween(first, second) {
    const radians = value => value * Math.PI / 180;
    const latitudeOne = radians(first[0]);
    const latitudeTwo = radians(second[0]);
    const longitudeDelta = radians(second[1] - first[1]);
    const y = Math.sin(longitudeDelta) * Math.cos(latitudeTwo);
    const x = Math.cos(latitudeOne) * Math.sin(latitudeTwo)
      - Math.sin(latitudeOne) * Math.cos(latitudeTwo) * Math.cos(longitudeDelta);
    return normalizeBearing(Math.atan2(y, x) * 180 / Math.PI);
  }

  function halfMileMaximumZoom(latitude) {
    const size = map.getSize();
    const shortestSide = Math.min(size.x, size.y);
    const latitudeScale = Math.max(Math.cos(latitude * Math.PI / 180), 0.01);
    const halfMileMeters = 804.672;
    const zoom = Math.log2(2 * Math.PI * 6378137 * latitudeScale * shortestSide
      / (256 * halfMileMeters));
    return Math.max(0, Math.min(17, Math.floor(zoom * 10) / 10));
  }

  function updateMaximumZoom(latitude) {
    const maximumZoom = halfMileMaximumZoom(latitude);
    map.options.maxZoom = maximumZoom;
    if (map.getZoom() > maximumZoom) map.setZoom(maximumZoom);
    return maximumZoom;
  }

  function updateTravelOrientation(location) {
    latestHeadingLocation = location;
    const position = coordinates(`${location?.latitude},${location?.longitude}`);
    if (!position) return;
    updateMaximumZoom(position[0]);

    const hasSpeed = typeof location.speed === 'number' && Number.isFinite(location.speed);
    const isMoving = !hasSpeed || location.speed >= 0.8;
    let heading = isMoving && Number.isFinite(location.heading)
      ? normalizeBearing(location.heading)
      : null;

    if (!isMoving) {
      lastBearingPosition = position;
    } else if (heading !== null) {
      lastBearingPosition = position;
    } else if (lastBearingPosition && distanceBetween(lastBearingPosition, position) >= 20) {
      heading = bearingBetween(lastBearingPosition, position);
      lastBearingPosition = position;
    } else if (!lastBearingPosition) {
      lastBearingPosition = position;
    }

    if (heading !== null) {
      if (smoothedBearing === null) {
        smoothedBearing = heading;
      } else {
        const difference = (heading - smoothedBearing + 540) % 360 - 180;
        smoothedBearing = normalizeBearing(smoothedBearing + difference * 0.25);
      }
      if (typeof map.setBearing === 'function') map.setBearing(normalizeBearing(-smoothedBearing));
    }
    map.panTo(position, {animate: false});
  }

  window.addEventListener('resize', () => {
    map.invalidateSize({pan: false});
    updateMaximumZoom(map.getCenter().lat);
  });

  function setStatus(message, isError = false) {
    status.textContent = message;
    status.style.color = isError ? '#a12d24' : '';
    status.hidden = !message;
  }

  async function stopDirectionsAudio() {
    directionsAudio?.pause();
    directionsAudio = null;
    if (directionsAudioUrl) URL.revokeObjectURL(directionsAudioUrl);
    directionsAudioUrl = '';
    playButton.textContent = 'Play first 2 turns';
    playButton.disabled = !maneuversList.children.length;
    if (parentMicrophonePaused) {
      parentMicrophonePaused = false;
      await window.parent.resumeMicrophoneAfterDirections?.();
    }
  }

  async function playFirstTwoSteps() {
    if (directionsAudio) {
      await stopDirectionsAudio();
      return;
    }
    const instructions = Array.from(maneuversList.querySelectorAll('.step-copy p'))
      .slice(0, 2)
      .map((item, index) => `Step ${index + 1}. ${item.textContent
        .replace(/\bft\./gi, 'feet')
        .replace(/\bFM-\s*/gi, 'Farm To Market ')
        .replace(/\bI-\s*/gi, 'Interstate ')
        .replace(/\bAve\./gi, 'Avenue.')
        .replace(/\bAve\b/gi, 'Avenue')
        .replace(/\bRd\./gi, 'Road.')
        .replace(/\bRd\b/gi, 'Road')
        .replace(/\bSt(?=\.|\s)/gi, 'Street')
        .replace(/\bTrl\./gi, 'Trail.')
        .replace(/\bTrl\b/gi, 'Trail')
        .replace(/\bW\./gi, 'West')
        .replace(/\bE\./gi, 'East')
        .replace(/\bN\./gi, 'North')
        .replace(/\bS\./gi, 'South')
        .replace(/\bPkwy\./gi, 'Parkway.')
        .replace(/\bPkwy\b/gi, 'Parkway')
        .replace(/\bExpy\./gi, 'Expressway.')
        .replace(/\bExpy\b/gi, 'Expressway')
        .replace(/\bTpke\./gi, 'Turnpike.')
        .replace(/\bTpke\b/gi, 'Turnpike')
        .replace(/\bFwy\./gi, 'Freeway.')
        .replace(/\bFwy\b/gi, 'Freeway')
        .replace(/\bmi\./gi, 'mile.')}`);
    if (!instructions.length) return;
    playButton.disabled = true;
    playButton.textContent = 'Preparing audio…';
    setStatus('Preparing spoken directions…');
    try {
      const response = await fetch('/api/speak', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({text: instructions.join(' ')}),
      });
      if (!response.ok) throw new Error('Unable to prepare spoken directions.');
      directionsAudioUrl = URL.createObjectURL(await response.blob());
      if (window.parent !== window && window.parent.pauseMicrophoneForDirections) {
        await window.parent.pauseMicrophoneForDirections();
        parentMicrophonePaused = true;
      }
      directionsAudio = new Audio(directionsAudioUrl);
      directionsAudio.addEventListener('ended', () => { void stopDirectionsAudio(); }, {once: true});
      directionsAudio.addEventListener('error', () => {
        void stopDirectionsAudio();
        setStatus('Unable to play spoken directions.', true);
      }, {once: true});
      playButton.textContent = 'Stop playback';
      playButton.disabled = false;
      await directionsAudio.play();
      setStatus('');
    } catch (error) {
      await stopDirectionsAudio();
      setStatus(error.message || 'Unable to play spoken directions.', true);
    }
  }

  function maybeStartAutomaticPlayback() {
    if (!playbackAllowed || !routeLoaded || automaticPlaybackStarted
      || !maneuversList.children.length) return;
    automaticPlaybackStarted = true;
    void playFirstTwoSteps();
  }

  window.addEventListener('message', event => {
    if (event.origin !== window.location.origin || event.source !== window.parent
      || event.data?.type !== 'mapquest:initial-playback-finished') return;
    playbackAllowed = true;
    maybeStartAutomaticPlayback();
  });

  window.addEventListener('message', event => {
    if (event.origin !== window.location.origin || event.source !== window.parent
      || event.data?.type !== 'mapquest:location-updated') return;
    const location = event.data.location;
    const origin = coordinates(`${location?.latitude},${location?.longitude}`);
    if (origin) {
      updateTravelOrientation(location);
      void showRoute(origin);
    }
  });

  window.addEventListener('message', event => {
    if (event.origin !== window.location.origin || event.source !== window.parent
      || event.data?.type !== 'mapquest:heading-updated') return;
    updateTravelOrientation(event.data.location);
  });

  async function showRoute(origin) {
    const requestVersion = ++routeRequestVersion;
    try {
      const response = await fetch('/api/mapquest/route', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({origin: origin.join(','), destination}),
      });
      const result = await response.json();
      if (requestVersion !== routeRequestVersion) return;
      if (!response.ok) throw new Error(result.detail || 'Could not load the MapQuest route.');
      if (!Array.isArray(result.points) || result.points.length < 2) {
        throw new Error('MapQuest returned no route geometry.');
      }

      const maneuvers = Array.isArray(result.maneuvers) ? result.maneuvers : [];
      const nextDirectionsSignature = directionsSignature(maneuvers);
      const directionsChanged = currentDirectionsSignature !== null
        && nextDirectionsSignature !== currentDirectionsSignature;
      if (directionsChanged) {
        await stopDirectionsAudio();
        automaticPlaybackStarted = false;
      }
      currentDirectionsSignature = nextDirectionsSignature;
      routeLayers.clearLayers();
      const route = L.polyline(result.points, {color: '#167a72', weight: 6, opacity: 0.9}).addTo(routeLayers);
      L.marker(result.points[0]).addTo(routeLayers).bindPopup('Start');
      L.marker(result.points[result.points.length - 1]).addTo(routeLayers).bindPopup('Destination');
      const upcomingTurns = maneuvers.slice(0, 3)
        .map(maneuver => coordinates(maneuver.point?.join(',')))
        .filter(Boolean);
      const nearTermPoints = upcomingTurns.length >= 2
        ? upcomingTurns
        : result.points.slice(0, Math.min(8, result.points.length));
      const maximumZoom = updateMaximumZoom(origin[0]);
      map.fitBounds(nearTermPoints, {padding: [6, 6], maxZoom: maximumZoom});
      if (latestHeadingLocation) updateTravelOrientation(latestHeadingLocation);
      const distance = Number(result.distance);
      summary.textContent = `${Number.isFinite(distance) ? `${distance.toFixed(1)} miles` : 'Route ready'}${result.formattedTime ? ` · ${result.formattedTime}` : ''}`;
      maneuversList.replaceChildren();
      for (const [index, maneuver] of maneuvers.entries()) {
        const item = document.createElement('li');
        const number = document.createElement('span');
        number.className = 'step-number';
        number.textContent = String(index + 1);
        const copy = document.createElement('div');
        copy.className = 'step-copy';
        const narrative = document.createElement('p');
        narrative.textContent = maneuver.narrative;
        const details = [
          typeof maneuver.distance === 'number' && Number.isFinite(maneuver.distance)
            ? `${maneuver.distance.toFixed(1)} mi`
            : '',
          maneuver.formattedTime || '',
        ].filter(Boolean);
        copy.append(narrative);
        if (details.length) {
          const metadata = document.createElement('small');
          metadata.textContent = details.join(' · ');
          copy.append(metadata);
        }
        item.append(number, copy);
        maneuversList.append(item);
      }
      playButton.disabled = !maneuvers.length;
      if (!maneuversList.children.length) {
        const item = document.createElement('li');
        item.textContent = 'No turn instructions were returned for this route.';
        maneuversList.append(item);
      }
      window.parent.postMessage({
        type: 'mapquest:navigation-snapshot',
        navigation: {
          origin: origin.join(','),
          destination,
          points: result.points,
          distance: Number.isFinite(distance) ? distance : null,
          formattedTime: result.formattedTime || '',
          maneuvers,
        },
      }, window.location.origin);
      setStatus('');
      routeLoaded = true;
      maybeStartAutomaticPlayback();
    } catch (error) {
      if (requestVersion !== routeRequestVersion) return;
      setStatus(error.message || 'Could not load the MapQuest route.', true);
      summary.textContent = 'Route unavailable';
    }
  }

  playButton.addEventListener('click', playFirstTwoSteps);

  if (!destination) {
    setStatus('No destination was provided.', true);
  } else {
    const origin = coordinates(parameters.get('from'));
    if (origin) {
      void showRoute(origin);
    } else if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        position => void showRoute([position.coords.latitude, position.coords.longitude]),
        () => setStatus('Allow location access to calculate directions from your current location.', true),
        {enableHighAccuracy: true, timeout: 15000, maximumAge: 30000},
      );
    } else {
      setStatus('This browser cannot provide a starting location.', true);
    }
  }
})();