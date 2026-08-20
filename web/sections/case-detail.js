(function(root){
  'use strict';

  var TYPES = { motion: true, order: true, notice: true, opinion: true, stipulation: true, other: true };

  function element(tag, className, text){
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function list(value){ return Array.isArray(value) ? value : []; }
  function typeOf(entry){
    var type = String((entry && entry.entry_type) || 'other').toLowerCase();
    return TYPES[type] ? type : 'other';
  }

  function setState(container, title, body, error){
    container.replaceChildren();
    var panel = element('div', 'state-panel glass' + (error ? ' error' : ''));
    panel.append(element('h2', '', title), element('p', '', body));
    container.append(panel);
  }

  function renderParties(container, parties){
    container.replaceChildren();
    if (!parties.length) return;

    var heading = element('h2', '', 'Parties & counsel');
    var partyList = element('div', 'party-list');
    parties.forEach(function(rawParty){
      var party = typeof rawParty === 'string' ? { name: rawParty } : (rawParty || {});
      var card = element('article', 'glass party-card');
      card.append(element('p', 'eyebrow', party.type || 'Party'));
      card.append(element('h3', '', party.name || 'Unnamed party'));
      var attorneys = list(party.attorneys);
      if (attorneys.length){
        var counsel = element('ul', 'attorney-list');
        attorneys.forEach(function(rawAttorney){
          var attorney = typeof rawAttorney === 'string' ? { name: rawAttorney } : (rawAttorney || {});
          var item = element('li');
          item.append(element('strong', '', attorney.name || 'Attorney'));
          var detail = [attorney.firm, attorney.email].filter(Boolean).join(' · ');
          if (detail) item.append(element('small', '', detail));
          counsel.append(item);
        });
        card.append(counsel);
      }
      partyList.append(card);
    });
    container.append(heading, partyList);
  }

  function renderTimeline(container, entries){
    container.replaceChildren();
    if (!entries.length){
      container.append(element('p', 'empty', 'No filings are cached for this case yet.'));
      return;
    }
    var timeline = element('ol', 'timeline-list');
    entries.forEach(function(entry){
      entry = entry || {};
      var item = element('li', 'timeline-entry glass');
      var meta = element('div', 'timeline-meta');
      var date = element('time', '', entry.date_filed || 'Date unknown');
      if (entry.date_filed) date.dateTime = entry.date_filed;
      meta.append(date, element('span', 'tag tag-' + typeOf(entry), typeOf(entry)));
      item.append(meta);
      var title = (entry.entry_number == null ? '' : 'No. ' + entry.entry_number + ' · ') + (entry.description || 'Untitled filing');
      item.append(element('h3', '', title));
      if (entry.filed_by) item.append(element('p', 'meta', 'Filed by ' + entry.filed_by));
      timeline.append(item);
    });
    container.append(timeline);
  }

  function renderDigest(container, docketId){
    setState(container, 'Loading digest', 'Building a plain-English summary of this case’s filings.');
    return root.DocketWatchApi.getDigest(docketId).then(function(data){
      container.replaceChildren();
      var block = element('div', 'digest-block');
      var lines = ((data && data.markdown) || '').split('\n').filter(function(line){ return line.trim(); });
      if (!lines.length){
        block.append(element('p', '', 'No digest available.'));
      } else {
        lines.forEach(function(line){ block.append(element('p', '', line)); });
      }
      container.replaceChildren(block);
    }).catch(function(error){
      setState(container, 'Could not load the digest', error.message || String(error), true);
    });
  }

  function renderAsk(container, docketId){
    container.replaceChildren();
    var form = document.createElement('form');
    form.className = 'ask-form';
    var input = document.createElement('input');
    input.type = 'text';
    input.placeholder = 'Ask a question about this case';
    input.setAttribute('aria-label', 'Question about this case');
    var button = element('button', 'go', 'Ask');
    button.type = 'submit';
    var answer = element('div', 'ask-answer');
    form.append(input, button);
    container.append(form, answer);
    form.addEventListener('submit', function(event){
      event.preventDefault();
      var question = input.value.trim();
      if (!question) return;
      button.disabled = true;
      setState(answer, 'Thinking', 'Asking the local model about this case.');
      root.DocketWatchApi.ask(docketId, question).then(function(result){
        answer.replaceChildren();
        answer.append(element('p', '', question), element('p', '', (result && result.answer) || ''));
      }).catch(function(error){
        setState(answer, 'Could not get an answer', error.message || String(error), true);
      }).finally(function(){
        button.disabled = false;
      });
    });
  }

  function render(docketId){
    var summary = document.getElementById('case-detail-summary');
    var timeline = document.getElementById('timeline-list');
    var title = document.getElementById('case-detail-title');
    var meta = document.getElementById('case-detail-meta');
    var digestContent = document.getElementById('digest-content');
    var askContent = document.getElementById('ask-content');
    if (!summary || !timeline || !title || !meta) return Promise.resolve();
    setState(summary, 'Loading case', 'Retrieving case information and the cached filing timeline.');
    timeline.replaceChildren();
    if (digestContent) digestContent.replaceChildren();
    if (askContent) askContent.replaceChildren();

    return Promise.all([root.DocketWatchApi.getCase(docketId), root.DocketWatchApi.getTimeline(docketId)])
      .then(function(results){
        var caseItem = results[0] || {};
        var timelinePayload = results[1] || {};
        title.textContent = caseItem.case_name || caseItem.docket_id || docketId;
        meta.textContent = [caseItem.docket_number, caseItem.court, caseItem.last_polled_at ? 'Last checked ' + caseItem.last_polled_at : ''].filter(Boolean).join(' · ') || 'Tracked case';
        renderParties(summary, list(caseItem.parties));
        renderTimeline(timeline, list(timelinePayload.entries));
        if (digestContent) renderDigest(digestContent, docketId);
        if (askContent) renderAsk(askContent, docketId);
      })
      .catch(function(error){
        title.textContent = 'Case detail';
        meta.textContent = 'The selected case could not be loaded.';
        setState(summary, 'Could not load this case', error.message || String(error), true);
        if (digestContent) setState(digestContent, 'Digest unavailable', 'This case could not be loaded.', true);
      });
  }

  root.DocketWatchCaseDetail = { render: render };
})(window);
