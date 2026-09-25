(() => {
  'use strict';
  const sidebar = document.querySelector('.docs-sidebar');
  const menu = document.querySelector('.menu-toggle');
  const search = document.querySelector('#docs-search');
  const links = [...document.querySelectorAll('.nav-group a')];
  const sections = [...document.querySelectorAll('main section[id]')];
  const status = document.querySelector('#copy-status');
  let statusTimer;

  function collapseMenu(collapsed) {
    sidebar.dataset.collapsed = String(collapsed);
    menu.setAttribute('aria-expanded', String(!collapsed));
  }
  collapseMenu(window.matchMedia('(max-width: 760px)').matches);
  menu.addEventListener('click', () => collapseMenu(sidebar.dataset.collapsed !== 'true'));
  links.forEach(link => link.addEventListener('click', () => {
    if (window.matchMedia('(max-width: 760px)').matches) collapseMenu(true);
  }));

  search.addEventListener('input', () => {
    const query = search.value.trim().toLocaleLowerCase('ru');
    let count = 0;
    links.forEach(link => {
      const section = document.getElementById(link.hash.slice(1));
      const text = `${link.textContent} ${section?.textContent || ''}`.toLocaleLowerCase('ru');
      link.hidden = !text.includes(query);
      if (!link.hidden) count++;
    });
    document.querySelectorAll('.nav-group').forEach(group => {
      group.hidden = [...group.querySelectorAll('a')].every(link => link.hidden);
    });
    document.querySelector('#search-empty').hidden = count > 0;
  });

  function updateCurrentSection() {
    const current = [...sections].reverse().find(section => section.getBoundingClientRect().top <= 130) || sections[0];
    links.forEach(link => {
      if (link.hash === `#${current.id}`) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
  }
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(updateCurrentSection, {rootMargin: '-72px 0px -65% 0px', threshold: [0, 1]});
    sections.forEach(section => observer.observe(section));
  }
  window.addEventListener('hashchange', updateCurrentSection);
  updateCurrentSection();

  document.querySelectorAll('pre').forEach(pre => {
    const code = pre.querySelector('code');
    if (!code) return;
    const label = document.createElement('span');
    label.className = 'code-label';
    label.textContent = pre.dataset.language || 'Пример';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'copy-button';
    button.textContent = 'Копировать';
    button.setAttribute('aria-label', `Копировать пример: ${label.textContent}`);
    button.addEventListener('click', async () => {
      clearTimeout(statusTimer);
      try {
        await navigator.clipboard.writeText(code.textContent);
        status.textContent = 'Пример скопирован';
      } catch {
        const range = document.createRange();
        range.selectNodeContents(code);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        status.textContent = 'Код выделен. Нажмите Ctrl+C или ⌘C, чтобы скопировать.';
      }
      statusTimer = setTimeout(() => { status.textContent = ''; }, 4500);
    });
    pre.append(label, button);
  });
})();
