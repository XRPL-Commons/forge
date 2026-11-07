export function factoryIdentity(value?: string | null) {
  const name = value?.trim() || 'Forge'
  const named = name.toLowerCase() !== 'forge'
  return { name, named, brand: named ? `${name} · Forge` : name }
}
