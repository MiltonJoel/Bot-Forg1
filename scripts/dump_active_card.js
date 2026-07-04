// Paste into the browser devtools console on https://www.forg1.com/predicciones
// the moment a prediction is live. Prints each element's tag/class/text so the
// placeholder selectors in app/bot/prediction_page.py can be replaced with real ones.
(() => {
  const idle = [...document.querySelectorAll("main h1,h2,h3")].some((h) =>
    h.textContent.includes("Esperando la próxima predicción"),
  );
  if (idle) {
    console.log("Still idle — run this again once a prediction is showing.");
    return;
  }
  const main = document.querySelector("main");
  const walk = (el, depth) => {
    if (depth > 6) return [];
    const cls = (el.className || "").toString().slice(0, 100);
    const text = el.childElementCount === 0 ? el.textContent.trim().slice(0, 50) : "";
    let out = [{ tag: el.tagName, cls, text }];
    for (const child of el.children) out = out.concat(walk(child, depth + 1));
    return out;
  };
  console.log(JSON.stringify(walk(main, 0), null, 1));
})();
