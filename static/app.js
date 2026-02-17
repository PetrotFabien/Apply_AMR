/* ==========================================================
   Raspi Stock – JS minimal (Thème A – stable)
   ========================================================== */

/**
 * Empêche le double submit sur un formulaire.
 * Appelé via onsubmit="disableOnSubmit(this)"
 */
function disableOnSubmit(form) {
    const btns = form.querySelectorAll("button[type='submit']");
    btns.forEach(btn => {
      btn.disabled = true;
    });
  }
  
  
  /**
   * Ferme une modale simple (Thème A)
   */
  function closeModal() {
    const m = document.querySelector('.modal-backdrop');
    if (m) m.remove();
  }
  
  
  /**
   * (Optionnel) Ouvre une modale simple
   * Utilisé uniquement si certains templates en ont besoin
   */
  function openModal(id) {
    const m = document.getElementById(id);
    if (m) m.style.display = 'flex';
  }