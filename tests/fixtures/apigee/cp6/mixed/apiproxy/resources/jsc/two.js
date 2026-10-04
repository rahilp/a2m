var n = Number(context.getVariable('response.header.X-Count') || '0');
context.setVariable('response.header.X-Count', String(n + 1));
