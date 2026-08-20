(function(root, factory){
  if (typeof module === 'object' && module.exports){
    module.exports = factory();
  } else {
    root.DocketWatchApi = factory();
  }
})(typeof self !== 'undefined' ? self : this, function(){
  'use strict';

  /** Error raised for network failures, non-JSON responses, and non-2xx API responses. */
  function ApiError(message, details){
    details = details || {};
    this.name = 'DocketWatchApiError';
    this.message = message;
    this.status = details.status || 0;
    this.url = details.url || '';
    this.payload = details.payload || null;
    this.cause = details.cause;
    if (Error.captureStackTrace) Error.captureStackTrace(this, ApiError);
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  function request(url, options){
    options = options || {};
    options.headers = Object.assign({ Accept: 'application/json' }, options.headers || {});

    return fetch(url, options).then(function(response){
      return response.text().then(function(rawBody){
        var payload = null;

        if (rawBody){
          try {
            payload = JSON.parse(rawBody);
          } catch (cause) {
            throw new ApiError('DocketWatch returned an invalid JSON response.', {
              status: response.status,
              url: url,
              cause: cause
            });
          }
        }

        if (!response.ok){
          var detail = payload && payload.error ? payload.error : (response.statusText || 'Request failed');
          throw new ApiError('DocketWatch request failed: ' + detail, {
            status: response.status,
            url: url,
            payload: payload
          });
        }

        return payload;
      });
    }, function(cause){
      throw new ApiError('Could not reach DocketWatch. Check that the local server is running.', {
        url: url,
        cause: cause
      });
    });
  }

  function requiredText(value, fieldName){
    if (typeof value !== 'string' || !value.trim()){
      throw new TypeError(fieldName + ' must be a non-empty string.');
    }
    return value.trim();
  }

  function caseUrl(docketId, suffix){
    var id = requiredText(docketId, 'docketId');
    return '/api/cases/' + encodeURIComponent(id) + (suffix || '');
  }

  function getCases(){
    return request('/api/cases');
  }

  function getCase(docketId){
    return request(caseUrl(docketId));
  }

  function getTimeline(docketId){
    return request(caseUrl(docketId, '/timeline'));
  }

  function getDigest(docketId){
    return request(caseUrl(docketId, '/digest'));
  }

  function ask(docketId, question){
    var id = requiredText(docketId, 'docketId');
    var prompt = requiredText(question, 'question');

    return request('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ docket_id: id, question: prompt })
    });
  }

  function trackCase(docketId){
    var id = requiredText(docketId, 'docketId');
    return request('/api/cases', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ docket_id: id })
    });
  }

  function untrackCase(docketId){
    return request(caseUrl(docketId), { method: 'DELETE' });
  }

  function search(query, limit){
    var q = requiredText(query, 'query');
    var url = '/api/search?q=' + encodeURIComponent(q);
    if (limit) url += '&limit=' + encodeURIComponent(limit);
    return request(url);
  }

  return {
    ApiError: ApiError,
    getCases: getCases,
    getCase: getCase,
    getTimeline: getTimeline,
    getDigest: getDigest,
    ask: ask,
    trackCase: trackCase,
    untrackCase: untrackCase,
    search: search
  };
});
