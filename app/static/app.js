function filterRows(inputId,tableId){const q=(document.getElementById(inputId)?.value||'').toLowerCase();const rows=document.querySelectorAll(`#${tableId} tbody tr`);if(rows.length){rows.forEach(r=>r.style.display=r.innerText.toLowerCase().includes(q)?'':'none');return}document.querySelectorAll(`#${tableId} > details`).forEach(folder=>folder.hidden=!folder.innerText.toLowerCase().includes(q))}

const adminTabs=document.querySelector('.admin-tabs');
if(adminTabs){
  const links=[...adminTabs.querySelectorAll('a[href^="#"]')];
  const sections=links.map(link=>document.getElementById(link.hash.slice(1))).filter(Boolean);
  const activateSection=()=>{
    const activeId=window.location.hash.slice(1);
    const activeSection=sections.find(section=>section.id===activeId)||sections[0];
    sections.forEach(section=>{section.hidden=section!==activeSection});
    links.forEach(link=>{
      if(link.hash===`#${activeSection.id}`)link.setAttribute('aria-current','page');
      else link.removeAttribute('aria-current');
    });
  };
  window.addEventListener('hashchange',activateSection);
  activateSection();
}
