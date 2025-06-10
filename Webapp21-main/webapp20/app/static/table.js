document.addEventListener("DOMContentLoaded", () => {
    const ths = document.querySelectorAll("th[data-field]");
    ths.forEach(th =>
        th.addEventListener("click", () => sortTable(th.dataset.field))
    );

    const monthSel = document.getElementById("monthFilter");
    if (monthSel) monthSel.addEventListener("change", () => {
        window.location = "?month=" + monthSel.value;
    });

    function sortTable(field) {
        const rows = Array.from(document.querySelectorAll("tbody tr"));
        const idx = { date: 0, type: 1, cat: 2, amt: 3 }[field];
        rows.sort((a, b) => {
            const t1 = a.children[idx].innerText;
            const t2 = b.children[idx].innerText;
            return field === "amt" ? (+t2.replace(/,/g, '')) - (+t1.replace(/,/g, '')) : t1.localeCompare(t2);
        });
        const tb = document.querySelector("tbody");
        tb.innerHTML = ""; rows.forEach(r => tb.appendChild(r));
    }
});