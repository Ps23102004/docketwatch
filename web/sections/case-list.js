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
        setState(container, 'No cases tracked yet', 'Track a docket with the DocketWatch CLI, then return here to inspect its filings.');
        return;
      }
      var detailLink = document.querySelector('[data-view-link="case"]');
      if (detailLink) detailLink.href = '#case/' + encodeURIComponent(cases[0].docket_id);
      cases.forEach(function(caseItem){
        var link = element('a', 'glass result-card case-card');
        link.href = '#case/' + encodeURIComponent(caseItem.docket_id);
        var heading = element('div', 'card-top');
        var name = element('div', 'card-title-group');
        name.append(element('h2', 'result-title', caseItem.case_name || caseItem.docket_id));
        name.append(element('p', 'result-meta', [caseItem.docket_number || 'No docket number', caseItem.court || 'Court not listed'].join(' · ')));
        heading.append(name, element('span', 'tag', String((caseItem.entries || []).length) + ' filings'));
        link.append(heading);
        container.append(link);
      });
    }).catch(function(error){
      setState(container, 'Could not load cases', error.message || String(error), true);
    }).finally(function(){
      container.setAttribute('aria-busy', 'false');
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
    root.addEventListener('hashchange', route);
    route();
  }

  root.DocketWatchCaseList = { render: renderCases, start: start };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})(window);
