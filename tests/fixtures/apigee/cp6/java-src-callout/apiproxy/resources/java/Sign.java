package com.example;

import com.apigee.flow.execution.ExecutionContext;
import com.apigee.flow.execution.ExecutionResult;
import com.apigee.flow.execution.spi.Execution;
import com.apigee.flow.message.MessageContext;

public class Sign implements Execution {
    public ExecutionResult execute(MessageContext messageContext, ExecutionContext executionContext) {
        String body = messageContext.getMessage().getContent();
        messageContext.setVariable("signature", Integer.toHexString(body.hashCode()));
        return ExecutionResult.SUCCESS;
    }
}
