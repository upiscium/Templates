set minimum-version := "1.55.0"
set shell := ["/bin/sh", "-cu"]

mod template 'just/template.just'
mod runtime 'just/runtime.just'
mod agent-core 'just/agent-core.just'
mod source 'just/source.just'

default:
    @just --list
