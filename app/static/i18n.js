// Local, curated UI translations. Never sends page content to a translation service.
(function () {
  'use strict';
  var catalog = window.USIRDTranslations || {};
  var reverse = Object.fromEntries(Object.entries(catalog).map(function (pair) { return [pair[1], pair[0]]; }));
  var patterns = [];
  function escapeRE(value) { return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
  Object.keys(catalog).forEach(function (key) {
    if (!/\{\w+\}/.test(key)) return;
    ['en', 'my'].forEach(function (lang) {
      var names = [], source = lang === 'my' ? catalog[key] : key, cursor = 0, regex = '^';
      source.replace(/\{(\w+)\}/g, function (match, name, offset) {
        regex += escapeRE(source.slice(cursor, offset)) + '(.+?)';
        names.push(name); cursor = offset + match.length; return match;
      });
      patterns.push({key:key,names:names,re:new RegExp(regex + escapeRE(source.slice(cursor)) + '$')});
    });
  });
  function language() { return document.documentElement.lang === 'my' ? 'my' : 'en'; }
  function canonical(text) { return reverse[text] || text; }
  function format(text, values) { return text.replace(/\{(\w+)\}/g, function (match, name) { return values[name] == null ? match : String(values[name]); }); }
  function t(text, values) {
    text = String(text || '');
    var key = canonical(text);
    if (!values && !catalog[key]) {
      for (var pattern of patterns) {
        var match = text.match(pattern.re);
        if (match) { key=pattern.key; values={}; pattern.names.forEach(function (name,index) { values[name]=match[index+1]; }); break; }
      }
    }
    return format(language() === 'my' ? (catalog[key] || text) : key, values || {});
  }
  window.USIRDLocale = {t:t,english:canonical,language:language};
  function repaint() {
    var skip = 'script,style,textarea,code,pre,[data-no-translate],[lang="en"][data-source-text]';
    var walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_TEXT);
    var node;
    while ((node=walker.nextNode())) {
      if (!node.parentElement || node.parentElement.closest(skip)) continue;
      var old=node.nodeValue, trimmed=old.trim();
      if (trimmed) node.nodeValue=old.replace(trimmed,t(trimmed));
    }
    document.querySelectorAll('[aria-label],[title],[placeholder],[data-confirm],[data-step-label]').forEach(function (element) {
      if (element.closest('[data-no-translate]')) return;
      ['aria-label','title','placeholder','data-confirm','data-step-label'].forEach(function (attribute) {
        if (element.hasAttribute(attribute)) element.setAttribute(attribute,t(element.getAttribute(attribute)));
      });
    });
    document.querySelectorAll('[data-language-switch] button').forEach(function (button) {
      button.setAttribute('aria-pressed',String(button.value===language()));
    });
    document.dispatchEvent(new CustomEvent('usird:language'));
  }
  var switching=false;
  document.querySelectorAll('[data-language-switch]').forEach(function (form) {
    form.addEventListener('submit',async function (event) {
      event.preventDefault();
      if (switching || !event.submitter) return;
      var target=event.submitter.value;
      if (target===language()) return;
      switching=true;
      var fields=new FormData(form);fields.set('language',target);
      form.setAttribute('aria-busy','true');
      try {
        var response=await fetch(form.action,{method:'POST',body:fields,credentials:'same-origin',headers:{Accept:'application/json'}});
        if (!response.ok) throw new Error('Language request failed');
        document.documentElement.lang=target;
        // Keep entered fields, the current step, focus, tabs, and payment iframe intact.
        repaint();
        if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
          document.querySelector('main').animate([{opacity:.85},{opacity:1}], {duration:140,easing:'ease-out'});
        }
        document.querySelector('[data-language-status]').textContent=t('Language changed.');
      } catch (_) {
        document.querySelector('[data-language-status]').textContent=t('Could not change language. Please try again.');
      } finally { switching=false;form.removeAttribute('aria-busy'); }
    });
  });
})();
