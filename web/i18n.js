// EV211 정적 화면의 한국어·영어 전환과 번역 문자열 치환을 제공한다.
(() => {
  const storageKey = 'ev211.language';

  function language() {
    try {
      return localStorage.getItem(storageKey) === 'en' ? 'en' : 'ko';
    } catch (_) {
      return 'ko';
    }
  }

  function interpolate(text, values) {
    return text.replace(/\{(\w+)\}/g, (_, key) => String(values[key] ?? ''));
  }

  window.EV211I18n = {
    language,
    create(strings, onChange = () => {}) {
      let currentLanguage = language();
      const t = (key, values = {}) => interpolate(strings[currentLanguage][key] || strings.ko[key] || key, values);
      const originalText = new WeakMap();
      const translateTextNodes = () => {
        if (!strings.text || !document.body) return;
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
          acceptNode(node) {
            return /^(SCRIPT|STYLE)$/i.test(node.parentElement?.tagName || '')
              ? NodeFilter.FILTER_REJECT
              : NodeFilter.FILTER_ACCEPT;
          },
        });
        let node;
        while ((node = walker.nextNode())) {
          const original = originalText.get(node) ?? node.textContent;
          originalText.set(node, original);
          const key = original.trim();
          const translated = strings.text[key]?.[currentLanguage];
          if (translated) node.textContent = original.replace(key, translated);
        }
      };
      const apply = () => {
        document.documentElement.lang = currentLanguage;
        document.querySelectorAll('[data-i18n]').forEach((element) => {
          element.textContent = t(element.dataset.i18n);
        });
        document.querySelectorAll('[data-i18n-placeholder]').forEach((element) => {
          element.placeholder = t(element.dataset.i18nPlaceholder);
        });
        document.querySelectorAll('[data-i18n-aria-label]').forEach((element) => {
          element.setAttribute('aria-label', t(element.dataset.i18nAriaLabel));
        });
        document.querySelectorAll('[data-i18n-title]').forEach((element) => {
          element.title = t(element.dataset.i18nTitle);
        });
        translateTextNodes();
        document.title = t('documentTitle');
        const toggle = document.getElementById('languageToggle');
        if (toggle) {
          toggle.textContent = currentLanguage === 'ko' ? 'English' : '한국어';
          toggle.setAttribute('aria-label', t('languageToggleLabel'));
        }
        onChange();
      };
      const setLanguage = (nextLanguage) => {
        currentLanguage = nextLanguage === 'en' ? 'en' : 'ko';
        try { localStorage.setItem(storageKey, currentLanguage); } catch (_) { /* 저장소가 제한된 브라우저에서는 이번 화면에만 적용한다. */ }
        apply();
      };
      const toggle = document.getElementById('languageToggle');
      if (toggle) toggle.onclick = () => setLanguage(currentLanguage === 'ko' ? 'en' : 'ko');
      apply();
      return { t, apply, setLanguage, get language() { return currentLanguage; } };
    },
  };
})();
