(function(root){
  'use strict';

  function element(tag, className, text){
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function setState(container, title, body, error){
    container.replaceChildren();
    var panel = element('div', 'state-panel glass' + (error ? ' error' : ''));
    panel.append(element('h2', '', title), element('p', '', body));
    container.append(panel);
  }

  function showView(name){
    document.querySelectorAll('.app-view').forEach(function(view){
      var active = view.dataset.view === name;
      view.hidden = !active;
      view.classList.toggle('is-active', active);
    });
    document.querySelectorAll('[data-view-link]').forEach(function(link){
      if (link.dataset.view === name) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    document.getElementById('main-content').focus({ preventScroll: true });
  }

  function renderCases(){
    var container = document.getElementById('case-list');
    if (!container) return Promise.resolve();
    container.setAttribute('aria-busy', 'true');
    setState(container, 'Loading cases', 'DocketWatch is reading the cases tracked on this device.');
    return root.DocketWatchApi.getCases().then(function(cases){
      cases = Array.isArray(cases) ? cases : [];
      container.replaceChildren();
      if (!cases.length){
        setState(container, 'No cases tracked yet', 'Track a docket above, or search below to find one.');
        return;
      }
      var detailLink = document.querySelector('[data-view-link="case"]');
      if (detailLink) detailLink.href = '#case/' + encodeURIComponent(cases[0].docket_id);
      cases.forEach(function(caseItem){
        var card = element('div', 'glass result-card case-card');
        var heading = element('div', 'card-top');
        var name = element('div', 'card-title-group');
        var titleLink = element('a', '');
        titleLink.href = '#case/' + encodeURIComponent(caseItem.docket_id);
        titleLink.append(element('h2', 'result-title', caseItem.case_name || caseItem.docket_id));
        name.append(titleLink);
        name.append(element('p', 'result-meta', [caseItem.docket_number || 'No docket number', caseItem.court || 'Court not listed'].join(' · ')));
        heading.append(name, element('span', 'tag', String((caseItem.entries || []).length) + ' filings'));
        var actions = element('div', 'card-actions');
        var removeBtn = element('button', 'detail-btn', 'Remove');
        removeBtn.type = 'button';
        removeBtn.addEventListener('click', function(){ removeCase(caseItem.docket_id); });
        actions.append(removeBtn);
        card.append(heading, actions);
        container.append(card);
      });
    }).catch(function(error){
      setState(container, 'Could not load cases', error.message || String(error), true);
    }).finally(function(){
      container.setAttribute('aria-busy', 'false');
    });
  }

  function removeCase(docketId){
    var status = document.getElementById('track-status');
    if (!window.confirm('Stop tracking ' + docketId + '? This deletes its cached filings.')) return;
    if (status) status.replaceChildren();
    root.DocketWatchApi.untrackCase(docketId).then(function(){
      return renderCases();
    }).catch(function(error){
      if (status) setState(status, 'Could not remove case', error.message || String(error), true);
    });
  }

  function initTrackForm(){
    var form = document.getElementById('track-form');
    var input = document.getElementById('track-input');
    var status = document.getElementById('track-status');
    if (!form || !input) return;
    form.addEventListener('submit', function(event){
      event.preventDefault();
      var docketId = input.value.trim();
      if (status) status.replaceChildren();
      if (!docketId) return;
      var button = form.querySelector('button');
      if (button) button.disabled = true;
      root.DocketWatchApi.trackCase(docketId).then(function(){
        input.value = '';
        return renderCases();
      }).catch(function(error){
        if (status) setState(status, 'Could not track that case', error.message || String(error), true);
      }).finally(function(){
        if (button) button.disabled = false;
      });
    });
  }

  function renderSearchResults(results){
    var container = document.getElementById('case-search-results');
    if (!container) return;
    container.replaceChildren();
    if (!results.length){
      setState(container, 'No matches', 'Nothing in the active source matched that search.');
      return;
    }
    results.forEach(function(hit){
      var card = element('div', 'glass result-card');
      var heading = element('div', 'card-top');
      var name = element('div', 'card-title-group');
      name.append(element('h2', 'result-title', hit.case_name || hit.id));
      name.append(element('p', 'result-meta', [hit.docket_number || 'No docket number', hit.court || 'Court not listed', hit.date_filed || 'Filed date unknown'].join(' · ')));
      heading.append(name, element('span', 'tag', hit.nature_of_suit || hit.source || 'case'));
      var actions = element('div', 'card-actions');
      var trackBtn = element('button', 'detail-btn', 'Track');
      trackBtn.type = 'button';
      trackBtn.addEventListener('click', function(){
        trackBtn.disabled = true;
        root.DocketWatchApi.trackCase(hit.id).then(function(){
          return renderCases();
        }).catch(function(error){
          setState(container, 'Could not track that case', error.message || String(error), true);
        }).finally(function(){
          trackBtn.disabled = false;
        });
      });
      actions.append(trackBtn);
      card.append(heading, actions);
      container.append(card);
    });
  }

  function initSearch(){
    var form = document.getElementById('case-search-form');
    var input = document.getElementById('case-search-input');
    var container = document.getElementById('case-search-results');
    if (!form || !input || !container) return;
    form.addEventListener('submit', function(event){ event.preventDefault(); });
    var timer = null;
    input.addEventListener('input', function(){
      window.clearTimeout(timer);
      var query = input.value.trim();
      if (!query){
        container.replaceChildren();
        return;
      }
      timer = window.setTimeout(function(){
        setState(container, 'Searching', 'Looking for cases matching "' + query + '".');
        root.DocketWatchApi.search(query).then(function(data){
          renderSearchResults((data && data.results) || []);
        }).catch(function(error){
          setState(container, 'Search failed', error.message || String(error), true);
        });
      }, 250);
    });
  }

  function route(){
    var match = location.hash.match(/^#case\/([^/]+)$/);
    if (match){
      var docketId;
      try { docketId = decodeURIComponent(match[1]); } catch (error) { docketId = ''; }
      if (docketId){
        showView('case');
        return root.DocketWatchCaseDetail.render(docketId);
      }
    }
    showView('cases');
    return renderCases();
  }

  function start(){
    if (!root.DocketWatchApi || !root.DocketWatchCaseDetail) return;
    initTrackForm();
    initSearch();
    root.addEventListener('hashchange', route);
    route();
  }

  root.DocketWatchCaseList = { render: renderCases, start: start };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})(window);
