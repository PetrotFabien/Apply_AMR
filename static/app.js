/* ============================================================================
   Thème B — JS complet (modales full-screen, animations, overlay, ESC)
   Compatible avec base.html + _modal.html
   Auteur : Copilot Fabien Edition :)
============================================================================ */


/* ============================================================================
   UTILITAIRE : Anti double-submit
============================================================================ */
function disableOnSubmit(form) {
    const btns = form.querySelectorAll("button[type='submit']");
    btns.forEach(btn => {
        btn.disabled = true;
        btn.classList.add("disabled");
    });
}


/* ============================================================================
   SÉLECTEURS GLOBAUX
============================================================================ */
const overlay   = document.getElementById("modal-overlay");
const container = document.getElementById("modal-container");


/* ============================================================================
   FERMETURE DE LA MODALE (public)
============================================================================ */
function closeModal() {
    if (!overlay || !container) return;

    // Animation sortie
    container.classList.remove("active");
    overlay.classList.remove("active");

    // Après animation → hide
    setTimeout(() => {
        container.innerHTML = "";
        overlay.classList.add("hidden");
        container.classList.add("hidden");
    }, 230);
}


/* ============================================================================
   OUVERTURE DE LA MODALE — depuis du HTML généré dynamiquement
============================================================================ */
function openModal(html) {
    if (!overlay || !container) return;

    // Injecte le contenu HTML du composant modal
    container.innerHTML = html;

    // Affiche overlay + conteneur
    overlay.classList.remove("hidden");
    container.classList.remove("hidden");

    // Active l'animation (fade + slide)
    requestAnimationFrame(() => {
        overlay.classList.add("active");
        container.classList.add("active");
    });

    bindModalEvents();
}


/* ============================================================================
   Si le serveur rend déjà la modale via Jinja :
   On détecte sa présence au chargement et on l’active automatiquement.
============================================================================ */
function openModalOnLoadIfPresent() {
    if (!overlay || !container) return;

    if (container.children.length > 0 && container.innerText.trim() !== "") {
        overlay.classList.remove("hidden");
        container.classList.remove("hidden");

        requestAnimationFrame(() => {
            overlay.classList.add("active");
            container.classList.add("active");
        });

        bindModalEvents();
    }
}


/* ============================================================================
   Gestion des événements modale
   - clic sur [data-modal-close]
   - clic sur overlay, hors du container
   - touche ESC
============================================================================ */
function bindModalEvents() {
    if (!overlay || !container) return;

    // Boutons data-modal-close
    const closers = container.querySelectorAll("[data-modal-close]");
    closers.forEach(btn => {
        btn.addEventListener("click", closeModal);
    });

    // Clic extérieur → fermer
    overlay.addEventListener("click", (e) => {
        const clickedInside = container.contains(e.target);
        if (!clickedInside) closeModal();
    });

    // ESC → fermer
    document.addEventListener("keydown", escHandler);
}

function escHandler(e) {
    if (e.key === "Escape") closeModal();
}
    //close Modals windows
function closeGlobalModal() {
    window.location = window.location.pathname;
}



/* ============================================================================
   READY → détecter modale server-side
============================================================================ */
document.addEventListener("DOMContentLoaded", () => {
    openModalOnLoadIfPresent();
});