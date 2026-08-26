/* ==========================================================================
   HostBot Landing Page - Client-side JS
   IntersectionObserver reveals, mobile nav, terminal typing, FAQ, status fetch
   ========================================================================== */

(function () {
  'use strict';

  // ---------- Navbar scroll effect ----------
  const navbar = document.getElementById('navbar');
  function onScroll() {
    if (window.scrollY > 40) {
      navbar.classList.add('scrolled');
    } else {
      navbar.classList.remove('scrolled');
    }
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // ---------- Mobile menu toggle ----------
  const mobileToggle = document.getElementById('mobile-toggle');
  const mobileMenu = document.getElementById('mobile-menu');
  let menuOpen = false;

  mobileToggle.addEventListener('click', function () {
    menuOpen = !menuOpen;
    mobileMenu.classList.toggle('hidden', !menuOpen);
    mobileToggle.innerHTML = menuOpen
      ? '<i class="ph-bold ph-x text-xl"></i>'
      : '<i class="ph-bold ph-list text-xl"></i>';
  });

  // Close mobile menu on link click
  mobileMenu.querySelectorAll('a').forEach(function (link) {
    link.addEventListener('click', function () {
      menuOpen = false;
      mobileMenu.classList.add('hidden');
      mobileToggle.innerHTML = '<i class="ph-bold ph-list text-xl"></i>';
    });
  });

  // ---------- Terminal typing animation ----------
  function initTerminal() {
    var lines = document.querySelectorAll('.terminal-line');
    lines.forEach(function (line) {
      var delay = parseInt(line.getAttribute('data-delay'), 10) || 0;
      line.style.animationDelay = delay + 'ms';
    });
  }
  initTerminal();

  // ---------- IntersectionObserver reveal ----------
  function initRevealObserver() {
    if (!('IntersectionObserver' in window)) {
      // Fallback: show everything
      document.querySelectorAll('.reveal-up, .reveal-vt').forEach(function (el) {
        el.style.opacity = '1';
        el.style.transform = 'none';
      });
      return;
    }

    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add('visible');
            observer.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.1, rootMargin: '0px 0px -40px 0px' }
    );

    // For .reveal-vt elements (scroll-triggered), observe them
    document.querySelectorAll('.reveal-vt').forEach(function (el) {
      observer.observe(el);
    });
  }
  initRevealObserver();

  // ---------- FAQ accordion ----------
  function initFAQ() {
    document.querySelectorAll('.faq-toggle').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var item = btn.closest('.faq-item');
        var isOpen = item.classList.contains('open');

        // Close all others
        document.querySelectorAll('.faq-item.open').forEach(function (openItem) {
          if (openItem !== item) {
            openItem.classList.remove('open');
            openItem.querySelector('.faq-toggle').setAttribute('aria-expanded', 'false');
          }
        });

        item.classList.toggle('open', !isOpen);
        btn.setAttribute('aria-expanded', String(!isOpen));
      });
    });
  }
  initFAQ();

  // ---------- Smooth scroll for anchor links ----------
  document.querySelectorAll('a[href^="#"]').forEach(function (anchor) {
    anchor.addEventListener('click', function (e) {
      var targetId = this.getAttribute('href');
      if (targetId === '#') return;
      var target = document.querySelector(targetId);
      if (target) {
        e.preventDefault();
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  });

  // ---------- Status fetch (optional) ----------
  // Fetches bot status from a VPS endpoint if available.
  // Expected response: { bots: number, running: number, uptime: string }
  function fetchStatus() {
    var statusEl = document.getElementById('live-status');
    if (!statusEl) return;

    fetch('/api/status')
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data.running !== undefined) {
          statusEl.textContent = data.running + ' bots online';
          statusEl.classList.add('visible');
        }
      })
      .catch(function () {
        // Status endpoint not available, hide indicator
        statusEl.style.display = 'none';
      });
  }
  fetchStatus();
})();
