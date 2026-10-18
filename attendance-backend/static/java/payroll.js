// =============================================
// SIDEBAR TOGGLE
// =============================================
function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    sidebar.classList.toggle('active');
}


// =============================================
// CONFIRM ACTIONS
// =============================================
function confirmAction(message) {
    return confirm(message || 'Are you sure?');
}

// =============================================
// FORMAT CURRENCY
// =============================================
function formatCurrency(amount) {
    return '₱' + parseFloat(amount).toLocaleString('en-PH', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    });
}

// =============================================
// FORMAT DATE
// =============================================
function formatDate(dateString) {
    const date = new Date(dateString);
    return date.toLocaleDateString('en-PH', {
        year: 'numeric',
        month: 'short',
        day: 'numeric'
    });
}

// =============================================
// PRINT
// =============================================
function printPage() {
    window.print();
}

