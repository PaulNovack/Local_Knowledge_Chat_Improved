(() => {
  const isLoopbackHost = ['localhost', '127.0.0.1'].includes(
    window.location.hostname.toLowerCase(),
  );
  const overridePanel = document.getElementById('location-override');
  const overrideEnabled = document.getElementById('override-location-enabled');
  const latitudeInput = document.getElementById('override-latitude');
  const longitudeInput = document.getElementById('override-longitude');
  const overrideStatus = document.getElementById('override-location-status');
  if (overridePanel) overridePanel.hidden = !isLoopbackHost;
  const overrideStorageKey = 'local-knowledge-chat.location-override';

  if (isLoopbackHost && overrideEnabled) {
    try {
      const saved = JSON.parse(localStorage.getItem(overrideStorageKey) || 'null');
      if (saved && typeof saved === 'object') {
        if (typeof saved.enabled === 'boolean') overrideEnabled.checked = saved.enabled;
        if (typeof saved.latitude === 'string' && latitudeInput) latitudeInput.value = saved.latitude;
        if (typeof saved.longitude === 'string' && longitudeInput) longitudeInput.value = saved.longitude;
      }
    } catch (error) {
      console.warn('Could not restore the saved location override.', error);
    }
  }

  const updateIntervalMs = 30_000;
  let browserLocation = null;
  let sending = false;
  let sentInitialLocation = false;
  let sendAgain = false;

  function saveOverrideState() {
    if (!isLoopbackHost || !overrideEnabled) return;
    try {
      localStorage.setItem(overrideStorageKey, JSON.stringify({
        enabled: overrideEnabled.checked,
        latitude: latitudeInput?.value || '',
        longitude: longitudeInput?.value || '',
      }));
    } catch (error) {
      console.warn('Could not save the location override preference.', error);
    }
  }

  function locationToSend() {
    if (!isLoopbackHost || !overrideEnabled?.checked) return browserLocation;

    const latitudeText = latitudeInput?.value.trim();
    const longitudeText = longitudeInput?.value.trim();
    const latitude = Number(latitudeText);
    const longitude = Number(longitudeText);
    const valid = latitudeText && longitudeText
      && Number.isFinite(latitude) && latitude >= -90 && latitude <= 90
      && Number.isFinite(longitude) && longitude >= -180 && longitude <= 180;
    latitudeInput?.setAttribute('aria-invalid', String(!valid));
    longitudeInput?.setAttribute('aria-invalid', String(!valid));
    if (!valid) {
      if (overrideStatus) overrideStatus.textContent = 'Enter valid latitude and longitude values.';
      return null;
    }
    if (overrideStatus) overrideStatus.textContent = 'Override location is active.';
    return {latitude, longitude};
  }

  async function sendLocation() {
    if (sending) {
      sendAgain = true;
      return;
    }
    const location = locationToSend();
    const clearLocation = isLoopbackHost && overrideEnabled && !overrideEnabled.checked && !browserLocation;
    if (!location && !clearLocation) return;
    sending = true;
    try {
      const response = await fetch('/api/geolocation', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify(location || {latitude: null, longitude: null}),
      });
      if (!response.ok) {
        console.warn(`Location update rejected by the server (${response.status}).`);
      }
    } catch (error) {
      console.warn('Could not send the location update; it will retry.', error);
    } finally {
      sending = false;
      if (sendAgain) {
        sendAgain = false;
        void sendLocation();
      }
    }
  }

  overrideEnabled?.addEventListener('change', () => {
    saveOverrideState();
    if (!overrideEnabled.checked && overrideStatus) overrideStatus.textContent = '';
    if (!overrideEnabled.checked && browserLocation) sentInitialLocation = true;
    void sendLocation();
  });
  for (const input of [latitudeInput, longitudeInput]) {
    input?.addEventListener('input', () => {
      saveOverrideState();
      if (isLoopbackHost && overrideEnabled?.checked) void sendLocation();
    });
  }
  if (isLoopbackHost && overrideEnabled?.checked) void sendLocation();

  if (!window.isSecureContext) {
    console.warn('Location tracking requires HTTPS or a localhost URL.');
  } else if (!navigator.geolocation) {
    console.warn('This browser does not provide geolocation.');
  } else {
    navigator.geolocation.watchPosition(position => {
      browserLocation = {
        latitude: position.coords.latitude,
        longitude: position.coords.longitude,
      };
      if (!sentInitialLocation && !(isLoopbackHost && overrideEnabled?.checked)) {
        sentInitialLocation = true;
        void sendLocation();
      }
    }, error => {
      const reasons = {
        1: 'Location permission was denied. Allow location access for this site in browser settings.',
        2: 'The computer could not determine its location. Check operating-system location services and Wi-Fi/location access.',
        3: 'The location lookup timed out. The browser will continue watching for a fix.',
      };
      console.warn(reasons[error.code] || 'Browser location lookup failed.', error.message);
    }, {enableHighAccuracy: true, maximumAge: 15_000, timeout: 20_000});
  }

  window.setInterval(() => void sendLocation(), updateIntervalMs);
})();