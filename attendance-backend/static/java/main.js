// =============================================
// MAIN.JS - FarmSync
// =============================================
console.log('🌾 FarmSync Main.js loaded');

// =============================================
// PAGE LOADER
// =============================================
function showLoader(text = 'Loading...') {
    let loader = document.getElementById('pageLoader');
    if (!loader) {
        loader = document.createElement('div');
        loader.id = 'pageLoader';
        loader.className = 'page-loader';
        loader.innerHTML = `
            <div class="loader-content">
                <div class="spinner spinner-large"></div>
                <p>${text}</p>
            </div>
        `;
        document.body.appendChild(loader);
    }
    setTimeout(() => loader.classList.add('active'), 10);
}

function hideLoader() {
    const loader = document.getElementById('pageLoader');
    if (loader) loader.classList.remove('active');
}

window.addEventListener('load', hideLoader);

// =============================================
// AUTO-HIDE ALERTS
// =============================================
document.addEventListener('DOMContentLoaded', function() {
    const alerts = document.querySelectorAll('.alert');
    alerts.forEach(alert => {
        setTimeout(() => {
            alert.style.transition = 'opacity 0.5s, transform 0.5s';
            alert.style.opacity = '0';
            alert.style.transform = 'translateY(-10px)';
            setTimeout(() => alert.remove(), 500);
        }, 5000);
    });
});

// =============================================
// FORM LOADING STATE
// =============================================
document.addEventListener('submit', function(e) {
    // Skip kung may preventDefault na
    if (e.defaultPrevented) return;

    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn && !submitBtn.classList.contains('btn-login') && !submitBtn.disabled) {
        submitBtn.classList.add('loading');
        submitBtn.disabled = true;
        setTimeout(() => {
            submitBtn.classList.remove('loading');
            submitBtn.disabled = false;
        }, 5000);
    }
});
console.log('✅ Main.js ready');