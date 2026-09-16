/* Theme toggle + password-field eye icon. */

function enableLightMode() {
    var themeToggle = document.getElementById('themeToggle');
    var head = document.head;
    if (!document.getElementById('lightTheme')) {
        var link = document.createElement('link');
        link.type = 'text/css';
        link.rel  = 'stylesheet';
        link.href = 'static/css/styles-light.css';
        link.id   = 'lightTheme';
        head.appendChild(link);
    }
    if (themeToggle) {
        themeToggle.classList.add('btn-dark');
        themeToggle.innerText = 'Dark Mode';
    }
    var toggler = document.getElementsByClassName('navbar-toggler')[0];
    if (toggler) toggler.classList.replace('navbar-dark', 'navbar-light');

    sessionStorage.setItem('mode', 'light');
}

function enableDarkMode() {
    var themeStylesheet = document.getElementById('lightTheme');
    var themeToggle     = document.getElementById('themeToggle');
    if (themeStylesheet) themeStylesheet.parentNode.removeChild(themeStylesheet);
    if (themeToggle) {
        themeToggle.classList.remove('btn-dark');
        themeToggle.innerText = 'Light Mode';
    }
    var toggler = document.getElementsByClassName('navbar-toggler')[0];
    if (toggler) toggler.classList.replace('navbar-light', 'navbar-dark');

    sessionStorage.setItem('mode', 'dark');
}

function toggleMode() {
    if (sessionStorage.getItem('mode') === 'dark') {
        enableLightMode();
    } else {
        enableDarkMode();
    }
}

document.addEventListener('DOMContentLoaded', function () {
    if (sessionStorage.getItem('mode') === 'light') {
        enableLightMode();
    } else {
        sessionStorage.setItem('mode', 'dark');
    }
});

function changePasswordVisibility() {
    var password       = document.getElementById('password');
    var togglePassword = document.getElementById('togglePassword');
    if (!password || !togglePassword) return;

    if (password.getAttribute('type') === 'password') {
        password.setAttribute('type', 'text');
        togglePassword.classList.add('fa-eye-slash');
    } else {
        password.setAttribute('type', 'password');
        togglePassword.classList.remove('fa-eye-slash');
    }
}

/* ---------------------------------------------------------------------------
   Category dropdowns in the top navigation.
   Click/tap to open (keyboard accessible), click-outside or Esc to close.
   Below 992px the panels are always expanded by CSS, so this only has to stop
   the placeholder link from navigating.
   --------------------------------------------------------------------------- */
document.addEventListener('DOMContentLoaded', function () {
    var menus = [].slice.call(document.querySelectorAll('[data-nav-menu]'));
    if (!menus.length) return;

    function isMobileNav() { return window.matchMedia('(max-width: 991px)').matches; }
    function closeAll(except) {
        menus.forEach(function (m) {
            if (m !== except) {
                m.classList.remove('open');
                var t = m.querySelector('.nav-link');
                if (t) t.setAttribute('aria-expanded', 'false');
            }
        });
    }

    menus.forEach(function (menu) {
        var trigger = menu.querySelector('.nav-link');
        if (!trigger) return;
        trigger.addEventListener('click', function (ev) {
            ev.preventDefault();
            if (isMobileNav()) return;          // panels are open already
            var willOpen = !menu.classList.contains('open');
            closeAll(menu);
            menu.classList.toggle('open', willOpen);
            trigger.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
        });
    });

    document.addEventListener('click', function (ev) {
        if (!ev.target.closest('[data-nav-menu]')) closeAll(null);
    });
    document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape') closeAll(null);
    });
});
